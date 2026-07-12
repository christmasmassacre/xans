# -*- coding: utf-8 -*-
"""
Mudah.my sender — отправка сообщений продавцам.
Использует Playwright persistent context (реальный Chrome-профиль).
Запуск: python sender.py

Все настройки берутся из config.txt (секция Sender).
Читает mudah_results.jsonl и шлёт сообщения тем, кому ещё не писали.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

import config
import db
import accounts


def resolve_profile() -> str:
    """
    Определяет, какой Chrome-профиль использовать.
    Приоритет: активный профиль из реестра (accounts.json) → config.txt.
    Так сендер автоматически подхватывает свежезарегистрированный аккаунт.
    """
    active = accounts.get_active()
    if active and os.path.exists(os.path.abspath(active)):
        print(f"[*] Активный профиль из реестра: {active}")
        return active
    if active:
        print(f"[!] Активный профиль {active} из реестра не найден на диске. "
              f"Использую config.txt.")
    return config.SENDER_CHROME_PROFILE


def load_listings() -> list[dict]:
    """Читает JSONL-файл с результатами парсера."""
    path = config.OUTPUT_FILE
    if not os.path.exists(path):
        print(f"[!] Файл {path} не найден. Сначала запусти парсер: python main.py")
        return []
    listings = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                listings.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return listings


def format_message(template: str, listing: dict) -> str:
    """Подставляет [Name] и другие переменные."""
    msg = template.replace("[Name]", listing.get("seller_name", "Seller").strip())
    try:
        return msg.format(
            title=listing.get("title", ""),
            price=listing.get("price", ""),
            seller_name=listing.get("seller_name", ""),
        )
    except (KeyError, IndexError):
        return msg


def send_message(page, chat_url: str, message: str, timeout_ms: int) -> tuple[bool, str]:
    """
    Открывает чат и отправляет сообщение.
    Возвращает (успех, причина).
    """
    try:
        page.goto(chat_url, wait_until="domcontentloaded", timeout=timeout_ms)
    except PwTimeout:
        return False, "timeout_goto"
    except Exception as e:
        return False, f"goto_error: {e}"

    # Ждём textarea
    try:
        textarea = page.wait_for_selector(
            'textarea[placeholder="Type a message"]',
            timeout=timeout_ms,
        )
    except PwTimeout:
        # Попробуем альтернативные селекторы
        try:
            textarea = page.wait_for_selector(
                'textarea, [contenteditable="true"], input[type="text"]',
                timeout=5000,
            )
        except PwTimeout:
            return False, "textarea_not_found"

    if not textarea:
        return False, "textarea_null"

    # Вписываем текст
    try:
        textarea.click()
        time.sleep(0.3)
        textarea.fill(message)
        time.sleep(0.3)
    except Exception as e:
        return False, f"fill_error: {e}"

    # Ищем кнопку SEND
    send_btn = None
    selectors = [
        'button:has-text("SEND")',
        'button:has-text("Send")',
        'button:has-text("send")',
        '[data-testid="send-button"]',
        'button[type="submit"]',
    ]
    for sel in selectors:
        try:
            send_btn = page.wait_for_selector(sel, timeout=3000)
            if send_btn:
                break
        except PwTimeout:
            continue

    if not send_btn:
        return False, "send_button_not_found"

    # Отправляем
    try:
        send_btn.click()
        # Ждём пока сообщение отправится (networkidle или просто 1 сек)
        page.wait_for_load_state("networkidle", timeout=3000)
    except Exception as e:
        # networkidle может отвалиться по таймауту, это нормально
        time.sleep(1)

    return True, "ok"


def main() -> None:
    import license
    license.require_license()

    import ensure_browser
    ensure_browser.ensure_chromium()

    db.init_db()

    # Тестовый лимит: если задан MUDAH_SEND_LIMIT, отправляем не больше N сообщений
    # и выходим (нужно для безопасной проверки, чтобы не заспамить всех сразу).
    send_limit = 0
    try:
        send_limit = int(os.environ.get("MUDAH_SEND_LIMIT", "0"))
    except ValueError:
        send_limit = 0
    sent_count = 0

    profile = resolve_profile()

    print(f"[*] Sender запущен (Press Ctrl+C to stop)")
    if send_limit > 0:
        print(f"    [ТЕСТ] Лимит отправки: {send_limit} сообщений, затем выход")
    print(f"    Сообщение: {config.SENDER_MESSAGE[:80]}...")
    print(f"    Chrome профиль: {profile}")
    print(f"    Headless: {config.SENDER_HEADLESS}")
    print()

    profile_path = os.path.abspath(profile)
    timeout_ms = config.SENDER_TIMEOUT * 1000

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch_persistent_context(
                user_data_dir=profile_path,
                headless=config.SENDER_HEADLESS,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            )

            page = browser.pages[0] if browser.pages else browser.new_page()

            while True:
                listings = load_listings()
                pending = []
                for item in listings:
                    lid = item.get("listing_id", "")
                    if not lid or db.is_message_sent(lid):
                        continue
                    chat_url = item.get("chat_url", "")
                    if chat_url:
                        pending.append(item)

                if not pending:
                    print("[*] Нет новых объявлений, ожидание 10 сек...")
                    time.sleep(10)
                    continue

                for i, item in enumerate(pending, 1):
                    lid = item.get("listing_id", "")
                    title = item.get("title", "")[:50]
                    chat_url = item.get("chat_url", "")
                    message = format_message(config.SENDER_MESSAGE, item)

                    print(f"[{i}/{len(pending)}] {title}")
                    print(f"    → {chat_url}")

                    ok, reason = send_message(page, chat_url, message, timeout_ms)

                    if ok:
                        db.mark_message_sent(lid, item.get("seller_name", ""), "sent")
                        print(f"    ✓ Отправлено")
                        sent_count += 1
                    else:
                        db.mark_message_sent(lid, item.get("seller_name", ""), f"error:{reason}")
                        print(f"    ✗ Ошибка: {reason}")

                    # Тестовый лимит достигнут — выходим
                    if send_limit > 0 and sent_count >= send_limit:
                        print(f"\n[ТЕСТ] Достигнут лимит {send_limit}. Завершаю.")
                        browser.close()
                        return

                    if config.SENDER_SLEEP > 0:
                        time.sleep(config.SENDER_SLEEP)
                        
                print("\n[*] Цикл отправки завершен. Проверяю новые...")
                
    except KeyboardInterrupt:
        print("\n[*] Sender остановлен пользователем.")


if __name__ == "__main__":
    main()
