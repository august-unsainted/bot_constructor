from pathlib import Path

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart

from BotConfig import BotConfig

router = Router()
bot_config = BotConfig(default_answer='эщкере')


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer_photo(**bot_config.messages.get('cmd_start'))


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(str(message.chat.id))
