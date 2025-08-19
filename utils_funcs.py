from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import sys
from pathlib import Path
from aiogram.types import FSInputFile


def get_btn(callback: str, text: str):
    return InlineKeyboardButton(callback_data=callback, text=text)


def generate_kb(back_callback: str = None, data: dict[str, str] = None) -> InlineKeyboardMarkup:
    def append_row(keyboard: list, callback: str, text: str = 'Назад ⬅️') -> None:
        keyboard.append([get_btn(callback, text)])
    kb = []
    if data:
        [append_row(kb, callback, text) for callback, text in data.items()]
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
        base_path = Path(__file__).parent.parent
    return str(base_path / relative_path)
