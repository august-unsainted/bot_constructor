import sys
import validators

from pathlib import Path
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton


def get_btn(callback: str, text: str) -> InlineKeyboardButton:
    key = 'url' if validators.url(callback) else 'callback_data'
    return InlineKeyboardButton(text=text, **{key: callback})


def append_row(keyboard: list, callback: str, text: str = 'Назад ⬅️', btns: dict = None) -> None:
    btns = btns or {callback: text}
    keyboard.append([get_btn(callback, text) for callback, text in btns.items()])


def generate_kb(back_callback: str = None, data: dict[str, str] = None) -> InlineKeyboardMarkup:
    kb = []
    if data:
        for callback, text in data.items():
            row_buttons = text if isinstance(text, dict) else None
            append_row(kb, callback, text, row_buttons)
    if back_callback:
        append_row(kb, back_callback)
    return InlineKeyboardMarkup(inline_keyboard=kb)


def create_input_file(path: Path | str) -> FSInputFile:
    path = find_resource_path(path)
    photo = FSInputFile(path=path)
    return photo


def find_resource_path(relative_path) -> str:
    try:
        base_path = Path(sys._MEIPASS)
    except AttributeError:
        base_path = Path.cwd()
    return str(base_path / relative_path)
