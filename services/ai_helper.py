import os
from typing import Optional

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

async def ask_homework_tutor(question: str, image_bytes: Optional[bytes] = None) -> str:
    """
    AI-репетитор для 4 класса
    """
    system_prompt = (
        "Ты добрый, терпеливый и понятный школьный репетитор для ученицы 4-го класса белорусской школы (9-10 лет). "
        "Твоя задача: помочь понять задание (по математике, русскому языку, белорусскому языку, предмету Человек и Мир и др.). "
        "Объясняй простыми словами, шаг за шагом, с добротой и примерами. Не используй сложные академические термины. "
        "Если это задача, покажи рассуждения и логику."
    )

    if not GEMINI_API_KEY:
        # Интеллектуальный ответ-заглушка с правилами 4 класса или инструкцией
        return (
            "💡 <b>Подсказка по урокам:</b>\n\n"
            "Чтобы подключить умный AI-репетитор (решение задач по фото и детальное объяснение правил), "
            "получите бесплатный ключ на <a href='https://aistudio.google.com/app/apikey'>Google AI Studio</a> "
            "и укажите его в боте.\n\n"
            f"Ваш запрос: <i>«{question}»</i> принят!"
        )

    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_API_KEY)
        if image_bytes:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[system_prompt, question, genai.types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")]
            )
        else:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[system_prompt, question]
            )
        return response.text
    except Exception as e:
        return f"❌ Ошибка AI-помощника: {e}"
