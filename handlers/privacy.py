"""
handlers/privacy.py — «удалите мои данные».

Право человека забрать свои данные не должно упираться в переписку с
поддержкой: он оставил их сам, сам и забирает. Стираются данные из заявок —
имя, телефон, машина, комментарий, — а сами заявки остаются обезличенными:
сервису они нужны как история загрузки, и ничего личного в них уже нет.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, Message

import keyboards as kb
from database import db

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("forget_me"), StateFilter(default_state))
async def forget_me(message: Message) -> None:
    await message.answer(
        "🗑 <b>Удаление ваших данных</b>\n\n"
        "Из ваших заявок пропадут имя, телефон, машина и комментарий. "
        "У сервисов останутся обезличенные записи о самом факте обращения — "
        "по ним вас не найти.\n"
        "Сохранённый телефон и профиль в боте тоже сотрём.\n\n"
        "Отменить это нельзя. Продолжить?",
        reply_markup=kb.kb_confirm("forget", "me"),
    )


@router.callback_query(F.data == "forget:me")
async def forget_confirm(callback: CallbackQuery) -> None:
    try:
        anonymized, active = await db.forget_client(callback.from_user.id)
    except Exception:
        logger.exception("Не удалось обезличить данные клиента %s", callback.from_user.id)
        await callback.message.edit_text(
            "⚠️ Не получилось: база не ответила. Попробуйте позже — "
            "просьба не потеряна, просто ещё не выполнена."
        )
        await callback.answer()
        return

    if active:
        # Стереть телефон и машину у заявки, по которой человека ждут завтра,
        # значит сорвать его же запись, а не защитить его данные
        await callback.message.edit_text(
            f"⚠️ У вас есть незакрытые заявки: {active}.\n\n"
            "Пока сервис вас ждёт, стереть данные нельзя. Отмените заявки "
            "в «📋 Мои заявки» или дождитесь их завершения и повторите "
            "/forget_me."
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        f"✅ Готово. Обезличено заявок: {anonymized}.\n\n"
        "Профиль появится снова, если вы продолжите пользоваться ботом: имя "
        "и @username бот получает от Telegram при каждом вашем сообщении."
    )
    await callback.answer("Данные удалены.")
