import os
import re
import time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# Кэш контактов и номеров
last_updates = {}  # contact_id -> {data, last_check, last_update}
cached_phones = {}  # phone -> (data, timestamp)

# ID кастомных полей в amoCRM для страны и региона
COUNTRY_FIELD_ID = 1390971
LOCATION_FIELD_ID = 1390973

# Если у вас есть кастомное поле для телефона, его ID.
# В полученных данных ключ custom_fields имеет id равное "1257175", можно его использовать.
PHONE_FIELD_ID = 1257175

def send_to_numverify(phone):
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 11 and digits.startswith("7"):
        number_to_send = digits[1:]
    else:
        number_to_send = digits

    print("Отправляем номер в numVerify:", number_to_send)
    now = time.time()
    cache_lifetime = 3600

    if number_to_send in cached_phones:
        data, timestamp = cached_phones[number_to_send]
        if now - timestamp < cache_lifetime:
            print("Используем кэш numVerify.")
            return data

    numverify_key = os.getenv('NUMVERIFY_ACCESS_KEY')
    if not numverify_key:
        raise Exception("NUMVERIFY_ACCESS_KEY не задан")

    url = f"http://apilayer.net/api/validate?access_key={numverify_key}&number={number_to_send}&country_code=RU&format=1"
    response = requests.get(url)

    if response.ok:
        data = response.json()
        if not data.get("valid", False):
            print("NumVerify: номер недействителен.")
            return {}
        cached_phones[number_to_send] = (data, now)
        return data
    else:
        raise Exception(f"Ошибка numVerify: {response.text}")

@app.route('/webhook', methods=['POST'])
def webhook():
    content_type = request.headers.get('Content-Type', '')
    if 'application/json' in content_type:
        data = request.get_json()
    elif 'application/x-www-form-urlencoded' in content_type:
        data = request.form.to_dict()
    else:
        data = request.data.decode('utf-8')

    print("Полученные данные:", data)

    contact_id = None
    phone = None

    # Определяем, обрабатываем обновление или создание нового контакта.
    if any(key.startswith("contacts[update]") for key in data.keys()):
        contact_id = data.get("contacts[update][0][id]")
        phone = data.get("contacts[update][0][custom_fields][0][values][0][value]")
    elif any(key.startswith("contacts[add]") for key in data.keys()):
        contact_id = data.get("contacts[add][0][id]")
        phone = data.get("contacts[add][0][custom_fields][0][values][0][value]")
    else:
        print("Нет данных контакта.")
        return jsonify({'status': 'error', 'message': "Недостаточно данных"}), 400

    if not contact_id or not phone:
        print("Нет ID контакта или номера телефона.")
        return jsonify({'status': 'error', 'message': "Недостаточно данных"}), 400

    try:
        phone_info = send_to_numverify(phone)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

    country = phone_info.get("country_name", "Не определено")
    location_value = phone_info.get("location", "").strip() or "Регион не определён"
    new_data = (phone, country, location_value)

    now_time = time.time()
    update_delay = 600  # 10 минут

    prev = last_updates.get(contact_id, {})
    prev_data = prev.get("data")
    prev_update_time = prev.get("last_update", 0)

    if prev_data == new_data and (now_time - prev_update_time) < update_delay:
        print(f"Контакт {contact_id}: данные те же, обновление недавно. Пропускаем.")
        # Обновляем время проверки, если требуется
        last_updates[contact_id]["last_check"] = now_time
        return jsonify({"status": "ok"}), 200

    AMO_DOMAIN = os.getenv("AMO_DOMAIN")
    ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")

    if contact_id and AMO_DOMAIN and ACCESS_TOKEN:
        update_url = f"https://{AMO_DOMAIN}.amocrm.ru/api/v4/contacts/{contact_id}?disable_webhooks=1"
        payload = {
            "custom_fields_values": [
                {"field_id": COUNTRY_FIELD_ID, "values": [{"value": country}]},
                {"field_id": LOCATION_FIELD_ID, "values": [{"value": location_value}]}
            ]
        }
        headers = {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        try:
            response = requests.patch(update_url, json=payload, headers=headers)
            print("Обновление контакта:", response.status_code)
            if response.ok:
                print("Контакт обновлён.")
                last_updates[contact_id] = {
                    "data": new_data,
                    "last_check": now_time,
                    "last_update": now_time
                }
            else:
                print("Ошибка Amo:", response.text)
        except Exception as e:
            print("Ошибка Amo API:", e)
    else:
        print("Нет данных авторизации или contact_id.")

    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)
