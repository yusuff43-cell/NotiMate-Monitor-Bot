"""Owner commands and scheduled digests, all in Russian regardless of input language."""

from __future__ import annotations

import datetime
import os

import pytz

import app
from logging_utils import get_logger
from notimate.projections.sheets import _money, upcoming_reminders

logger = get_logger()


def weekly_report(client_cfg):
    """Еженедельная аналитика — воскресенье 18:00"""
    if not app.gc:
        return
    try:
        tz = pytz.timezone('Asia/Bangkok')
        now = datetime.datetime.now(tz)
        week_ago = (now - datetime.timedelta(days=7)).strftime('%Y-%m-%d')
        date_today = now.strftime('%Y-%m-%d')
        sh = app.gc.open_by_key(client_cfg['sheet_id'])

        # Выручка за неделю
        total_revenue = 0
        try:
            ws_rev = sh.worksheet('Выручка')
            rows_rev = ws_rev.get_all_records()
            week_rev = [r for r in rows_rev if str(r.get('Дата','')).strip()[:10] >= week_ago]
            total_revenue = sum(float(str(r.get('Gross Sales',0) or 0).replace('฿','').replace(',','').strip() or 0) for r in week_rev)
        except: pass

        # Расходы за неделю
        total_expenses = 0
        try:
            ws_exp = sh.worksheet('Расходы')
            rows_exp = ws_exp.get_all_records()
            week_exp = [r for r in rows_exp if str(r.get('Дата','')).strip()[:10] >= week_ago]
            def safe_float(v):
                try: return float(str(v or 0).replace('฿','').replace(',','').replace('B','').strip() or 0)
                except: return 0
            total_expenses = sum(safe_float(r.get('Сумма (THB)',0)) for r in week_exp)
        except: pass

        # Проблемы за неделю
        problems_text = ''
        try:
            ws_prob = sh.worksheet('Проблемы')
            rows_prob = ws_prob.get_all_records()
            week_prob = [r for r in rows_prob if str(r.get('Дата','')).strip()[:10] >= week_ago]
            if week_prob:
                problems_text = '\n'.join([f"- {r.get('Сообщение','')[:50]}" for r in week_prob[-3:]])
        except: pass

        # Критичные остатки
        out_of_stock = []
        try:
            ws_ost = sh.worksheet('Остатки')
            rows_ost = ws_ost.get_all_records()
            last_date = max([r.get('Дата','') for r in rows_ost if r.get('Дата','')], default='')
            if last_date:
                last_rows = [r for r in rows_ost if str(r.get('Дата','')) == last_date or not r.get('Дата','')]
                out_of_stock = [r.get('Продукт','') for r in last_rows if r.get('Примечание','') == 'Out of stock']
        except: pass

        profit = total_revenue - total_expenses
        profit_sign = '+' if profit >= 0 else ''

        report = app.ask_openai(
            'Составь короткую еженедельную сводку для русскоязычного владельца кофейни. Пиши только на русском, конкретно и кратко, максимум 15 строк.',
            f"""Данные за неделю ({week_ago} — {date_today}):
Выручка: {total_revenue:.0f} THB
Расходы: {total_expenses:.0f} THB
Прибыль: {profit_sign}{profit:.0f} THB
Проблемы: {problems_text or 'не зафиксировано'}
Закончилось: {', '.join(out_of_stock[:5]) or 'всё в норме'}

Формат:
📊 Итоги недели [даты]
💰 Выручка: X THB
💸 Расходы: X THB
📈 Прибыль: X THB
⚠️ Проблемы: список или 'нет'
🔴 Закончилось: список или 'всё ок'
💡 Вывод: 1-2 предложения""",
            800,
        )
        app.notify_owner(client_cfg, report)
    except Exception as exc:
        logger.error('weekly_report_failed', extra={'error_type': type(exc).__name__})


def postgres_period_totals(tenant_id, now):
    """(revenue_today, revenue_month, expenses_today, expenses_month) from the Этап 4 ledger,
    or None when PostgreSQL can't answer — the caller then falls back to Sheets, so switching
    the source can never leave the owner without a summary."""
    reader = getattr(app, 'dashboard_reader', None)
    if not (tenant_id and reader):
        return None
    try:
        today = now.date()
        totals = reader.daily_totals(tenant_id, today.replace(day=1), today, False)
    except Exception as exc:
        logger.warning('postgres_report_source_failed', extra={'error_type': type(exc).__name__})
        return None
    day = totals.get(today.isoformat(), {})
    return (
        day.get('revenue', 0.0), sum(v['revenue'] for v in totals.values()),
        day.get('expenses', 0.0), sum(v['expenses'] for v in totals.values()),
    )


def evening_summary(client_cfg, tenant_id=None):
    """Короткая сводка для владельца в 20:00 по Бангкоку.

    Numbers come from Sheets by default. ``REPORTS_SOURCE=postgres`` (Этап 4 switch-over,
    enabled only after ``deploy/compare_sheets_vs_postgres.py`` shows a week of matching
    data) reads the PostgreSQL ledger instead, with Sheets as the fallback.
    """
    if not app.gc:
        return
    try:
        tz = pytz.timezone('Asia/Bangkok')
        now = datetime.datetime.now(tz)
        date_today = now.strftime('%Y-%m-%d')
        month_prefix = now.strftime('%Y-%m')
        sh = app.gc.open_by_key(client_cfg['sheet_id'])

        revenue_today = revenue_month = expenses_today = expenses_month = 0.0
        pg_totals = postgres_period_totals(tenant_id, now) if os.environ.get('REPORTS_SOURCE') == 'postgres' else None
        if pg_totals:
            revenue_today, revenue_month, expenses_today, expenses_month = pg_totals
        else:
            try:
                rows = sh.worksheet('Выручка').get_all_records()
                revenue_today = sum(_money(row.get('Gross Sales')) for row in rows if str(row.get('Дата', '')).startswith(date_today))
                revenue_month = sum(_money(row.get('Gross Sales')) for row in rows if str(row.get('Дата', '')).startswith(month_prefix))
            except Exception:
                pass
            try:
                rows = sh.worksheet('Расходы').get_all_records()
                expenses_today = sum(_money(row.get('Сумма (THB)')) for row in rows if str(row.get('Дата', '')).startswith(date_today))
                expenses_month = sum(_money(row.get('Сумма (THB)')) for row in rows if str(row.get('Дата', '')).startswith(month_prefix))
            except Exception:
                pass

        balance_today = revenue_today - expenses_today
        msg = (
            f"🌙 Вечерняя сводка · {now.strftime('%d.%m.%Y')}\n\n"
            f"💰 Выручка сегодня: {revenue_today:,.0f} THB\n"
            f"💸 Расходы сегодня: {expenses_today:,.0f} THB\n"
            f"📈 Разница за день: {balance_today:+,.0f} THB\n"
            f"📊 За месяц: выручка {revenue_month:,.0f} · расходы {expenses_month:,.0f} THB"
        )
        reminders = upcoming_reminders(sh, now)
        if reminders:
            msg += '\n\n🔔 Ближайшие напоминания:'
            for left, title, expiry in reminders:
                when = 'сегодня' if left == 0 else f'через {left} дн.'
                msg += f"\n• {title} — {when} ({expiry})"
        else:
            msg += '\n\n🔔 Ближайших напоминаний нет.'
        app.notify_owner(client_cfg, msg)
    except Exception as exc:
        logger.error('evening_summary_failed', extra={'error_type': type(exc).__name__})


def reminders_report(client_cfg):
    if not app.gc:
        return
    try:
        now = datetime.datetime.now(pytz.timezone('Asia/Bangkok'))
        reminders = upcoming_reminders(app.gc.open_by_key(client_cfg['sheet_id']), now, days_limit=7, limit=12)
        if reminders:
            lines = ['🔔 Напоминания на ближайшие 7 дней:']
            for left, title, expiry in reminders:
                when = 'сегодня' if left == 0 else f'через {left} дн.'
                lines.append(f"• {title} — {when} ({expiry})")
            msg = '\n'.join(lines)
        else:
            msg = '🔔 На ближайшие 7 дней напоминаний нет.'
        app.notify_owner(client_cfg, msg)
    except Exception as exc:
        logger.error('reminders_report_failed', extra={'error_type': type(exc).__name__})


def owner_menu(client_cfg):
    app.notify_owner(client_cfg, 'Постоянное меню находится внизу чата. Нажмите «Отчёты», чтобы открыть его.')


def detailed_report(client_cfg):
    """Развёрнутый отчёт по запросу владельца."""
    if not app.gc:
        return
    try:
        tz = pytz.timezone('Asia/Bangkok')
        now = datetime.datetime.now(tz)
        date_today = now.strftime('%Y-%m-%d')
        yesterday = (now - datetime.timedelta(days=1)).strftime('%Y-%m-%d')
        sh = app.gc.open_by_key(client_cfg['sheet_id'])
        try:
            all_rows = sh.worksheet('Остатки').get_all_records()
            today_rows = [row for row in all_rows if str(row.get('Дата', '')) == date_today] or all_rows[-50:]
        except Exception:
            today_rows = []
        try:
            rows_exp = sh.worksheet('Расходы').get_all_records()
            total_yesterday = sum(_money(row.get('Сумма (THB)')) for row in rows_exp if str(row.get('Дата', '')) == yesterday)
            total_month = sum(_money(row.get('Сумма (THB)')) for row in rows_exp if str(row.get('Дата', '')).startswith(now.strftime('%Y-%m')))
        except Exception:
            total_yesterday = total_month = 0.0
        stock_text = '\n'.join(
            f"{row.get('Продукт', '')} | Холодильник: {row.get('Холодильник', '')} | Морозилка: {row.get('Морозилка', '')} | {row.get('Примечание', '')}"
            for row in today_rows if row.get('Продукт')
        )
        report = app.ask_openai(
            "Ты аналитик кафе. Составь подробный отчёт только на русском для владельца. Будь конкретным и компактным. Формат: 📋 Подробный отчёт по кофейне [дата]; 🔴 ЗАКОНЧИЛОСЬ / КРИТИЧНО; 🟡 МАЛО ОСТАЛОСЬ; 💰 РАСХОДЫ ВЧЕРА; 📊 РАСХОДЫ ЗА МЕСЯЦ; 💡 РЕКОМЕНДАЦИИ.",
            f"Дата: {date_today}\nОстатки:\n{stock_text}\nРасходы вчера: {total_yesterday} THB\nРасходы за месяц: {total_month} THB",
            1000,
        )
        reminders = upcoming_reminders(sh, now, days_limit=7)
        if reminders:
            report += '\n\n🔔 ВАЖНЫЕ ДОКУМЕНТЫ:'
            for left, title, expiry in reminders:
                report += f"\n⚠️ {title} — истекает через {left} дн. ({expiry})"
        app.notify_owner(client_cfg, report)
    except Exception as exc:
        logger.error('detailed_report_failed', extra={'error_type': type(exc).__name__})
