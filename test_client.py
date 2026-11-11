import sys
import getpass
from schools_client import SchoolsClient

def main():
    print("=== Тестирование подключения к Schools.by ===")
    if len(sys.argv) >= 3:
        login = sys.argv[1]
        password = sys.argv[2]
    else:
        login = input("Введите логин от schools.by: ").strip()
        password = getpass.getpass("Введите пароль: ").strip()

    client = SchoolsClient()
    print(f"\n[1/3] Попытка входа для пользователя '{login}'...")
    if not client.login(login, password):
        print(" Ошибка: Не удалось войти. Проверьте логин и пароль.")
        return

    print(" Успешная авторизация!")
    print(f"\n[2/3] Обнаруженные ученики в аккаунте: {len(client.pupils)}")
    for i, p in enumerate(client.pupils, 1):
        print(f"  {i}. {p['name']} (ID: {p['id']}, Поддомен школы: {p['subdomain']})")

    if not client.pupils:
        print("Внимание: ученики не были автоматически распознаны на главной странице.")
        # Можно вручную запросить ID
        manual_id = input("Введите ID ученика вручную (из ссылки https://...schools.by/pupil/ID): ").strip()
        manual_sub = input("Введите поддомен школы (например, 199minsk): ").strip()
        client.pupils.append({"id": manual_id, "name": "Ученик", "subdomain": manual_sub})

    target_pupil = client.pupils[0]
    print(f"\n[3/3] Получение дневника за текущую неделю для {target_pupil['name']}...")
    days = client.get_week_diary(target_pupil["id"], target_pupil["subdomain"])

    if not days:
        print("Дневник пуст или разметка требует уточнения.")
    else:
        for day in days:
            print(f"\n--- {day['title']} ---")
            for les in day["lessons"]:
                mark_str = f" [Оценка: {les['mark']}]" if les['mark'] else ""
                hw_str = f" | ДЗ: {les['homework']}" if les['homework'] else ""
                print(f"  {les['number']}. {les['subject']}{hw_str}{mark_str}")

    print("\n--- Проверка четвертных оценок ---")
    q_marks = client.get_quarter_marks(target_pupil["id"], target_pupil["subdomain"])
    if q_marks:
        for q in q_marks:
            print(f"  {q['subject']}: I={q['q1']} | II={q['q2']} | III={q['q3']} | IV={q['q4']} | Год={q['year']}")
    else:
        print("Четвертные оценки не найдены.")

if __name__ == "__main__":
    main()
