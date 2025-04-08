import os
import re
import time
import requests
from dotenv import load_dotenv

# Загружаем переменные окружения из файла .env
load_dotenv()

# Глобальные кэши:
# last_updates хранит для каждого контакта данные последнего обновления:
#     {data: (phone, country, location), last_check: timestamp, last_update: timestamp}
last_updates = {}  # contact_id -> {data, last_check, last_update}
# cached_phones кэширует ответы API numverify для ускорения работы
cached_phones = {}  # phone -> (data, timestamp)

# ID кастомных полей в AmoCRM для страны и региона
COUNTRY_FIELD_ID = 1390971
LOCATION_FIELD_ID = 1390973

# Код поля телефона в AmoCRM
PHONE_FIELD_CODE = "PHONE"


def send_to_numverify(phone):
    """
    Отправляет номер в API numverify и кэширует результат на 1 час.
    Приводит номер к нужному формату (если 11 цифр и начинается на 7, убирает первую цифру).
    """
    digits = re.sub(r'\D', '', phone)
    if len(digits) == 11 and digits.startswith("7"):
        number_to_send = digits[1:]
    else:
        number_to_send = digits

    now = time.time()
    cache_lifetime = 3600  # кэш действителен 1 час
    if number_to_send in cached_phones:
        data, timestamp = cached_phones[number_to_send]
        if now - timestamp < cache_lifetime:
            return data

    numverify_key = os.getenv('NUMVERIFY_ACCESS_KEY')
    if not numverify_key:
        raise Exception("NUMVERIFY_ACCESS_KEY не задан")

    url = f"http://apilayer.net/api/validate?access_key={numverify_key}&number={number_to_send}&country_code=RU&format=1"
    response = requests.get(url)
    if response.ok:
        data = response.json()
        if not data.get("valid", False):
            return {}
        cached_phones[number_to_send] = (data, now)
        return data
    else:
        raise Exception(f"Ошибка numVerify: {response.text}")


def get_phone_from_contact(contact):
    """
    Извлекает номер телефона из контакта AmoCRM.
    Предполагается, что номер хранится в custom_fields_values с field_code равным "PHONE".
    """
    custom_fields = contact.get('custom_fields_values') or []
    for field in custom_fields:
        if field.get('field_code') == PHONE_FIELD_CODE:
            values = field.get('values', [])
            if values:
                return values[0].get('value')
    return None


def update_contact(contact_id, country, location_value):
    """
    Обновляет контакт в AmoCRM, записывая страну и регион в указанные кастомные поля.
    """
    AMO_DOMAIN = os.getenv('AMO_DOMAIN')
    ACCESS_TOKEN = os.getenv('ACCESS_TOKEN')
    if not contact_id or not AMO_DOMAIN or not ACCESS_TOKEN:
        return

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
        if not response.ok:
            print(f"Ошибка Amo при обновлении контакта {contact_id}: {response.text}")
    except Exception as e:
        print(f"Ошибка Amo API при обновлении контакта {contact_id}: {e}")


def poll_contacts():
    """
    Получает список контактов из AmoCRM.
    Используется пагинация (если предусмотрена API).
    """
    AMO_DOMAIN = os.getenv('AMO_DOMAIN')
    ACCESS_TOKEN = os.getenv('ACCESS_TOKEN')
    if not AMO_DOMAIN or not ACCESS_TOKEN:
        raise Exception("Нет данных авторизации AmoCRM (AMO_DOMAIN или ACCESS_TOKEN)")

    url = f"https://{AMO_DOMAIN}.amocrm.ru/api/v4/contacts"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    contacts = []
    while url:
        response = requests.get(url, headers=headers)
        if response.ok:
            data = response.json()
            contacts_page = data.get('_embedded', {}).get('contacts', [])
            contacts.extend(contacts_page)
            links = data.get('_links', {})
            next_link = links.get('next', {}).get('href') if links.get('next') else None
            url = next_link
        else:
            print("Ошибка Amo при запросе контактов:", response.text)
            break
    return contacts


def process_contact(contact):
    """
    Проверяет изменение номера телефона и, если необходимо, обновляет контакт в AmoCRM.
    Вывод происходит только если для контакта выполнено обновление.
    """
    contact_id = contact.get("id")
    if not contact_id:
        return

    phone = get_phone_from_contact(contact)
    if not phone:
        return

    now_time = time.time()
    update_delay = 600  # 10 минут

    previous = last_updates.get(contact_id, {})
    prev_data = previous.get('data')
    if prev_data and prev_data[0] == phone and (now_time - previous.get('last_update', 0)) < update_delay:
        last_updates[contact_id]['last_check'] = now_time
        return

    try:
        phone_info = send_to_numverify(phone)
    except Exception as e:
        print(f"Контакт {contact_id}: Ошибка numVerify - {e}")
        return

    country = phone_info.get("country_name", "Не определено")
    location_value = phone_info.get("location", "").strip() or "Регион не определён"

    update_contact(contact_id, country, location_value)
    last_updates[contact_id] = {
        'data': (phone, country, location_value),
        'last_update': now_time,
        'last_check': now_time
    }
    print(f"Контакт {contact_id}: обновлено. Телефон: {phone}, страна: {country}, регион: {location_value}")


def main_loop():
    """
    Основной цикл опроса AmoCRM. Каждые 30 секунд запрашивается список контактов и производится их обработка.
    Вывод подробной информации происходит только для контактов, для которых выполнено обновление.
    """
    while True:
        print("Опрос AmoCRM контактов...")
        try:
            contacts = poll_contacts()
            print(f"Найдено контактов: {len(contacts)}")
            for contact in contacts:
                process_contact(contact)
        except Exception as e:
            print("Ошибка при опросе контактов:", e)
        print("Ожидание 30 секунд до следующего опроса.")
        time.sleep(30)


if __name__ == '__main__':
    main_loop()
