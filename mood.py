import random

import database


async def random_walk_mood() -> int:
    """Случайно сдвигает настроение бота (0-100) и сохраняет в БД. Вызывается по расписанию."""
    current = await database.get_mood()
    delta = random.randint(-20, 20)
    new_value = max(0, min(100, current + delta))
    await database.set_mood(new_value)
    return new_value


def mood_label(value: int) -> str:
    if value < 20:
        return "подавленное, вялое"
    if value < 40:
        return "немного грустное"
    if value < 60:
        return "нейтральное, спокойное"
    if value < 80:
        return "хорошее, бодрое"
    return "отличное, восторженное"


def reaction_probability(value: int, prob_min: int, prob_max: int) -> float:
    """Чем выше настроение, тем выше шанс, что бот отреагирует на пост."""
    span = prob_max - prob_min
    prob_percent = prob_min + span * (value / 100)
    return prob_percent / 100
