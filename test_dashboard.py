"""Этап 8: signed links, snapshot assembly, and the Flask routes' auth/tenant isolation.
The PostgreSQL SQL itself is covered by test_dashboard_postgres.py (needs TEST_DATABASE_URL)."""

import datetime as dt
import importlib
import io
import json
import os
import unittest
import zipfile
from unittest.mock import Mock, patch

os.environ.setdefault('OPENAI_API_KEY', 'test-key')
os.environ.setdefault('GOOGLE_CREDENTIALS', '{}')
os.environ.setdefault('DISABLE_SCHEDULER', '1')
os.environ.setdefault('CLIENTS_JSON', json.dumps({
    'Ubot': {
        'channel_access_token': 'test-token',
        'channel_secret': 'test-secret',
        'owner_line_id': 'Uowner',
        'sheet_id': 'test-sheet',
    }
}))

app_module = importlib.import_module('app')

from notimate.dashboard import links  # noqa: E402
from notimate.dashboard.reader import build_snapshot  # noqa: E402

SECRET = 'unit-test-secret'


class LinkTests(unittest.TestCase):
    def test_round_trip(self):
        token = links.issue_token(SECRET, 't1', 'owner', 'link', 60, now=1000)
        claims = links.verify_token(SECRET, token, 'link', now=1030)
        self.assertEqual((claims['t'], claims['s']), ('t1', 'owner'))

    def test_expired_wrong_scope_wrong_secret_and_tampered_are_rejected(self):
        token = links.issue_token(SECRET, 't1', 'owner', 'link', 60, now=1000)
        self.assertIsNone(links.verify_token(SECRET, token, 'link', now=1061))
        self.assertIsNone(links.verify_token(SECRET, token, 'session', now=1010))
        self.assertIsNone(links.verify_token('other', token, 'link', now=1010))
        body, sig = token.split('.')
        forged = links._b64(json.dumps({'t': 't2', 's': 'owner', 'sc': 'link', 'e': 9999999999}).encode()) + '.' + sig
        self.assertIsNone(links.verify_token(SECRET, forged, 'link', now=1010))
        for junk in ('', 'abc', 'a.b.c', body + '.'):
            self.assertIsNone(links.verify_token(SECRET, junk, 'link', now=1010))

    def test_link_token_cannot_be_used_as_session(self):
        token = links.issue_token(SECRET, 't1', 'owner', 'link', 60)
        self.assertIsNone(links.verify_token(SECRET, token, 'session'))

    def test_unknown_scope_and_empty_secret_refuse_to_issue(self):
        with self.assertRaises(ValueError):
            links.issue_token(SECRET, 't', 's', 'admin', 10)
        with self.assertRaises(ValueError):
            links.issue_token('', 't', 's', 'link', 10)


class FakeReader:
    def daily_totals(self, tenant_id, start, end, location_pack):
        self.args = (tenant_id, start, end, location_pack)
        return {'2026-09-24': {'revenue': 1000.0, 'expenses': 300.0}, '2026-09-10': {'revenue': 500.0, 'expenses': 50.0}, '2026-08-30': {'revenue': 9999.0, 'expenses': 9999.0}}

    def payments_for(self, tenant_id, day, location_pack):
        return {'cash': 400.0, 'card': 500.0, 'qr': 100.0}

    def critical_stock(self, tenant_id, since):
        return [{'product': 'Лосось', 'amount': '1 кг', 'status': 'low'}]

    def deadlines(self, tenant_id, today, days=14):
        return []

    def problems(self, tenant_id, since):
        return []

    def recent_operations(self, tenant_id, location_pack):
        return []

    def expense_rows(self, tenant_id, start, end, location_pack):
        self.expense_args = (tenant_id, start, end, location_pack)
        return [
            {'day': dt.date(2026, 9, 24), 'operation_type': 'expense', 'counterparty': 'Makro', 'description': 'молоко', 'amount': 300},
            {'day': dt.date(2026, 9, 23), 'operation_type': 'expense', 'counterparty': '', 'description': 'аренда точки', 'amount': 700},
            {'day': dt.date(2026, 9, 23), 'operation_type': 'salary', 'counterparty': 'Аня', 'description': '', 'amount': 500},
            {'day': dt.date(2026, 9, 22), 'operation_type': 'expense', 'counterparty': 'Неизвестно', 'description': 'zzz', 'amount': 100},
        ]

    def location_rows(self, tenant_id, start, end):
        return [{'id': 'l1', 'name': 'Точка 1', 'revenue': 900, 'cash': 400, 'non_cash': 500, 'payouts': 50, 'reports': 2}]


class CategoryTests(unittest.TestCase):
    def test_keyword_rules_and_fallback(self):
        from notimate.dashboard.categories import categorize
        self.assertEqual(categorize('Makro', 'milk'), 'Продукты и сырьё')
        self.assertEqual(categorize('', 'аренда за сентябрь'), 'Аренда')
        self.assertEqual(categorize('Grab', ''), 'Транспорт и доставка')
        self.assertEqual(categorize('', 'кофе в зёрнах'), 'Напитки')
        self.assertEqual(categorize('Аня', '', 'salary'), 'Зарплаты')
        self.assertEqual(categorize('X', 'что-то странное'), 'Прочее')
        self.assertEqual(categorize(None, None), 'Прочее')

    def test_tenant_overrides_win(self):
        from notimate.dashboard.categories import categorize
        self.assertEqual(categorize('Makro', 'milk', 'expense', {'Кухня': ['makro']}), 'Кухня')
        self.assertEqual(categorize('Makro', 'milk', 'expense', {'Кухня': 'not-a-list'}), 'Продукты и сырьё')


class SnapshotTests(unittest.TestCase):
    def test_contract_shape_and_month_scoping(self):
        now = dt.datetime(2026, 9, 24, 21, 0)
        reader = FakeReader()
        snap = build_snapshot(reader, {'id': 't1', 'country': 'KZ', 'timezone': 'Asia/Almaty', 'vertical_pack': None}, 'day', now)
        for key in ('generatedAt', 'timezone', 'currency', 'period', 'today', 'month', 'payments', 'trend', 'criticalStock', 'deadlines', 'problems', 'recentOperations'):
            self.assertIn(key, snap)
        self.assertEqual(snap['currency'], 'KZT')
        self.assertEqual(snap['today'], {'revenue': 1000.0, 'expenses': 300.0, 'result': 700.0})
        # the 2026-08-30 row lies outside September and must not leak into the month total
        self.assertEqual(snap['month'], {'revenue': 1500.0, 'expenses': 350.0, 'result': 1150.0})
        self.assertEqual(len(snap['trend']), 14)
        self.assertEqual(snap['trend'][-1]['date'], '2026-09-24')
        self.assertEqual(snap['trend'][0]['revenue'], 0.0)
        self.assertEqual([p['label'] for p in snap['payments']], ['cash', 'card', 'qr'])
        self.assertEqual(reader.args[0], 't1')

    def test_period_selection_and_category_breakdown(self):
        now = dt.datetime(2026, 9, 24, 21, 0)
        week = build_snapshot(FakeReader(), {'id': 't1', 'name': 'Кафе', 'sheet_id': 'SID', 'channels': ['line']}, 'week', now)
        self.assertEqual(week['selected']['from'], '2026-09-18')
        self.assertEqual(week['channels'], ['line'])
        self.assertEqual(week['sheetUrl'], 'https://docs.google.com/spreadsheets/d/SID')
        cats = {c['category']: c for c in week['expensesByCategory']}
        self.assertEqual(set(cats), {'Продукты и сырьё', 'Аренда', 'Зарплаты', 'Прочее'})
        self.assertEqual(cats['Аренда']['amount'], 700.0)
        self.assertAlmostEqual(sum(c['share'] for c in cats.values()), 1.0, places=3)
        self.assertEqual(week['expensesByCategory'][0]['category'], 'Аренда')  # sorted by amount
        self.assertEqual(len(week['expenses']), 4)
        day = build_snapshot(FakeReader(), {'id': 't1'}, 'day', now)
        self.assertEqual(day['selected']['from'], '2026-09-24')

    def test_location_tenant_gets_per_location_rows(self):
        snap = build_snapshot(FakeReader(), {'id': 'e', 'vertical_pack': 'location_reports'}, 'day', dt.datetime(2026, 9, 24))
        self.assertEqual(snap['locations'], [{'name': 'Точка 1', 'revenue': 900.0, 'cash': 400.0, 'nonCash': 500.0, 'payouts': 50.0, 'reports': 2}])

    def test_location_pack_flag_reaches_reader(self):
        reader = FakeReader()
        build_snapshot(reader, {'id': 'e', 'vertical_pack': 'location_reports'}, 'month', dt.datetime(2026, 9, 24))
        self.assertTrue(reader.args[3])


class RouteTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'DASHBOARD_LINK_SECRET': SECRET, 'DASHBOARD_API_BASE': 'https://api.example', 'DASHBOARD_ORIGIN': 'https://dash.example', 'DASHBOARD_BASE_URL': 'https://dash.example/'})
        env.start()
        self.addCleanup(env.stop)
        self.orig = {name: getattr(app_module, name) for name in ('tenant_store', 'TENANTS_DB_ENABLED', 'dashboard_reader', 'documents_store', 'DOCUMENTS_DB_ENABLED')}
        self.addCleanup(lambda: [setattr(app_module, k, v) for k, v in self.orig.items()])
        self.tenants = {
            'a': {'id': 'a', 'name': 'A', 'country': 'KZ', 'timezone': 'Asia/Almaty', 'vertical_pack': 'location_reports', 'modules': {}, 'owner_ids': ['ownerA']},
            'b': {'id': 'b', 'name': 'B', 'country': 'TH', 'timezone': 'Asia/Bangkok', 'vertical_pack': None, 'modules': {'accountant': {'accountant_ids': ['accB']}}, 'owner_ids': ['ownerB']},
        }
        store = Mock()
        store.get_tenant.side_effect = lambda tid: self.tenants.get(tid)
        app_module.tenant_store = store
        app_module.TENANTS_DB_ENABLED = True
        self.reader = FakeReader()
        app_module.dashboard_reader = self.reader
        self.client = app_module.app.test_client()

    def login(self, tenant='a', subject='ownerA'):
        token = links.issue_token(SECRET, tenant, subject, 'link', 60)
        return self.client.get(f'/auth/link?t={token}')

    def test_feature_is_off_without_secret(self):
        with patch.dict(os.environ, {'DASHBOARD_LINK_SECRET': ''}):
            self.assertEqual(self.client.get('/v1/owner-dashboard').status_code, 404)

    def test_no_session_is_401(self):
        response = self.client.get('/v1/owner-dashboard')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_login_sets_httponly_cookie_and_api_returns_snapshot(self):
        response = self.login()
        self.assertEqual(response.status_code, 302)
        cookie = response.headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('Secure', cookie)
        self.assertIn('SameSite=Lax', cookie)
        self.assertNotIn('link', links.verify_token(SECRET, cookie.split('nm_session=')[1].split(';')[0], 'session')['sc'])
        api = self.client.get('/v1/owner-dashboard?period=week', headers={'Origin': 'https://dash.example'})
        self.assertEqual(api.status_code, 200)
        self.assertEqual(api.headers['Cache-Control'], 'no-store')
        self.assertEqual(api.headers['Access-Control-Allow-Origin'], 'https://dash.example')
        self.assertEqual(api.headers['Access-Control-Allow-Credentials'], 'true')
        self.assertEqual(api.get_json()['period'], 'week')
        self.assertEqual(self.reader.args[0], 'a')

    def test_other_origin_gets_no_cors_headers(self):
        self.login()
        api = self.client.get('/v1/owner-dashboard', headers={'Origin': 'https://evil.example'})
        self.assertNotIn('Access-Control-Allow-Origin', api.headers)

    def test_tenant_comes_only_from_session_never_from_query(self):
        self.login()
        self.client.get('/v1/owner-dashboard?tenant=b&tenant_id=b')
        self.assertEqual(self.reader.args[0], 'a')

    def test_subject_not_an_owner_cannot_log_in(self):
        self.assertEqual(self.login('a', 'someone-else').status_code, 403)
        self.assertEqual(self.login('missing', 'ownerA').status_code, 403)

    def test_removed_owner_loses_existing_session(self):
        self.login()
        self.tenants['a']['owner_ids'] = []
        self.assertEqual(self.client.get('/v1/owner-dashboard').status_code, 401)

    def test_session_token_cannot_be_used_as_login_link_and_vice_versa(self):
        session = links.issue_token(SECRET, 'a', 'ownerA', 'session', 60)
        self.assertEqual(self.client.get(f'/auth/link?t={session}').status_code, 403)
        link = links.issue_token(SECRET, 'a', 'ownerA', 'link', 60)
        self.client.set_cookie('nm_session', link)
        self.assertEqual(self.client.get('/v1/owner-dashboard').status_code, 401)

    def test_invalid_period_is_400(self):
        self.login()
        self.assertEqual(self.client.get('/v1/owner-dashboard?period=year').status_code, 400)

    def test_snapshot_failure_is_503_without_detail(self):
        self.login()
        self.reader.daily_totals = Mock(side_effect=RuntimeError('secret db detail'))
        response = self.client.get('/v1/owner-dashboard')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('secret', response.get_data(as_text=True))

    def test_package_download_for_owner_and_accountant_only(self):
        store = Mock()
        store.list_confirmed.return_value = []
        store.list_operations.return_value = []
        store.open_questions.return_value = []
        app_module.documents_store = store
        app_module.DOCUMENTS_DB_ENABLED = True
        ok = self.client.get('/d/package?t=' + links.issue_token(SECRET, 'b', 'accB', 'package', 60, '2026-09'))
        self.assertEqual(ok.status_code, 200)
        self.assertIn('2026-09/реестр.csv', zipfile.ZipFile(io.BytesIO(ok.data)).namelist())
        self.assertEqual(ok.headers['Cache-Control'], 'no-store')
        stranger = self.client.get('/d/package?t=' + links.issue_token(SECRET, 'b', 'nobody', 'package', 60, '2026-09'))
        self.assertEqual(stranger.status_code, 403)
        bad_period = self.client.get('/d/package?t=' + links.issue_token(SECRET, 'b', 'ownerB', 'package', 60, '../../x'))
        self.assertEqual(bad_period.status_code, 403)
        wrong_scope = self.client.get('/d/package?t=' + links.issue_token(SECRET, 'b', 'ownerB', 'link', 60, '2026-09'))
        self.assertEqual(wrong_scope.status_code, 403)

    def test_page_and_script_are_served_with_strict_headers(self):
        page = self.client.get('/dashboard')
        self.assertEqual(page.status_code, 200)
        self.assertIn('text/html', page.headers['Content-Type'])
        self.assertIn("script-src 'self'", page.headers['Content-Security-Policy'])
        self.assertIn("frame-ancestors 'none'", page.headers['Content-Security-Policy'])
        self.assertEqual(page.headers['Cache-Control'], 'no-store')
        script = self.client.get('/dashboard/app.js')
        self.assertEqual(script.status_code, 200)
        self.assertIn('javascript', script.headers['Content-Type'])
        self.assertNotIn(b'innerHTML', script.data)  # data is only ever inserted as text
        with patch.dict(os.environ, {'DASHBOARD_LINK_SECRET': ''}):
            self.assertEqual(self.client.get('/dashboard').status_code, 404)

    def test_login_without_base_url_lands_on_the_dashboard_page(self):
        with patch.dict(os.environ, {'DASHBOARD_BASE_URL': ''}):
            response = self.login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/dashboard')

    def test_snapshot_includes_accounting_block_with_package_link_when_module_on(self):
        store = Mock()
        store.list_confirmed.return_value = [{'doc_number': '2026-09-001', 'doc_type': 'receipt_simplified', 'seller': 'Makro', 'total': 100,
                                              'doc_date': '2026-09-10', 'subtotal': None, 'vat': None, 'tax_id': '', 'doc_ref': '', 'image_sha256': None}]
        store.list_operations.return_value = []
        store.period_status.return_value = 'sent'
        app_module.documents_store = store
        app_module.DOCUMENTS_DB_ENABLED = True
        self.tenants['b']['modules'] = {'accountant': {'enabled': True}}
        self.login('b', 'ownerB')
        snap = self.client.get('/v1/owner-dashboard').get_json()
        block = snap['accounting']
        self.assertEqual((block['available'], block['documents']), (True, 1))
        self.assertEqual(block['previousStatus'], 'sent')
        self.assertTrue(block['packageUrl'].startswith('https://api.example/d/package?t='))
        self.assertGreaterEqual(block['missing'], 1)  # simplified receipt in a VAT-registered TH tenant
        self.assertEqual(snap['channels'], [])
        self.tenants['a']['modules'] = {}
        self.login('a', 'ownerA')
        self.assertIsNone(self.client.get('/v1/owner-dashboard').get_json()['accounting'])

    def test_owner_link_helpers_respect_pack_and_feature_flag(self):
        from notimate.dashboard import routes
        self.assertTrue(routes.owner_link(self.tenants['a'], 'ownerA').startswith('https://api.example/auth/link?t='))
        self.assertIsNone(routes.owner_link(self.tenants['b'], 'ownerB'))  # not a dashboard pack, not opted in
        opted_in = {**self.tenants['b'], 'modules': {'dashboard': {'enabled': True}}}
        self.assertIsNotNone(routes.owner_link(opted_in, 'ownerB'))
        with patch.dict(os.environ, {'DASHBOARD_LINK_SECRET': ''}):
            self.assertIsNone(routes.owner_link(self.tenants['a'], 'ownerA'))


class ReportsSourceTests(unittest.TestCase):
    """REPORTS_SOURCE=postgres: evening summary reads the ledger, falls back to Sheets."""

    def run_summary(self, env, reader):
        from notimate.reports import summaries
        sent = []
        sheet = Mock()
        sheet.worksheet.side_effect = Exception('no sheet in this test')
        gc = Mock()
        gc.open_by_key.return_value = sheet
        orig_reader = getattr(app_module, 'dashboard_reader', None)
        self.addCleanup(setattr, app_module, 'dashboard_reader', orig_reader)
        app_module.dashboard_reader = reader
        with patch.dict(os.environ, env), patch.object(app_module, 'gc', gc), \
                patch.object(app_module, 'notify_owner', side_effect=lambda cfg, msg: sent.append(msg)), \
                patch.object(summaries, 'upcoming_reminders', return_value=[]):
            summaries.evening_summary({'sheet_id': 'x'}, tenant_id='t1')
        return sent[0]

    def test_postgres_source_is_used_when_switched_on(self):
        today = dt.datetime.now(dt.timezone(dt.timedelta(hours=7))).date().isoformat()
        reader = Mock()
        reader.daily_totals.return_value = {today: {'revenue': 1234.0, 'expenses': 234.0}}
        message = self.run_summary({'REPORTS_SOURCE': 'postgres'}, reader)
        self.assertIn('Выручка сегодня: 1,234 THB', message)
        self.assertIn('Расходы сегодня: 234 THB', message)

    def test_default_source_ignores_postgres(self):
        reader = Mock()
        message = self.run_summary({'REPORTS_SOURCE': ''}, reader)
        reader.daily_totals.assert_not_called()
        self.assertIn('Выручка сегодня: 0 THB', message)

    def test_postgres_failure_falls_back_to_sheets_path(self):
        reader = Mock()
        reader.daily_totals.side_effect = RuntimeError('db down')
        message = self.run_summary({'REPORTS_SOURCE': 'postgres'}, reader)
        self.assertIn('Вечерняя сводка', message)


class RetireOverviewTests(unittest.TestCase):
    def test_next_working_tab_skips_overview_and_hidden_tabs(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('retire_overview_sheet', os.path.join(os.path.dirname(__file__), 'deploy', 'retire_overview_sheet.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tab = lambda title, gid, hidden=False: Mock(title=title, id=gid, isSheetHidden=hidden)
        tabs = [tab('Закупки', 1), tab('Обзор', 2), tab('Скрытая', 3, True), tab('Выручка', 4), tab('Расходы', 5)]
        self.assertEqual(module.next_working_gid(tabs), 4)
        self.assertEqual(module.next_working_gid([tab('Обзор', 2), tab('Закупки', 1)]), 1)  # wraps around
        self.assertIsNone(module.next_working_gid([tab('Обзор', 2)]))


class LineDashboardLinkTests(unittest.TestCase):
    def test_owner_report_command_appends_dashboard_link_only_when_enabled(self):
        from notimate import pipeline
        cfg = {'channel_access_token': 't', 'owner_line_id': 'Uo', 'sheet_id': 's', 'name': 'JSC', 'modules': {'dashboard': {'enabled': True}}}
        event = {'type': 'message', 'source': {'type': 'user', 'userId': 'Uo'}, 'message': {'type': 'text', 'text': 'подробный отчёт'}}
        orig_sheets = app_module.SHEETS_ENABLED
        app_module.SHEETS_ENABLED = True
        self.addCleanup(setattr, app_module, 'SHEETS_ENABLED', orig_sheets)
        with patch.dict(os.environ, {'DASHBOARD_LINK_SECRET': SECRET, 'DASHBOARD_API_BASE': 'https://api.example'}), \
                patch.object(app_module, 'find_client', return_value=cfg), \
                patch.object(app_module, 'detailed_report') as report, \
                patch.object(app_module, 'notify_owner') as notify:
            pipeline.process_line_event('dest', event)
            report.assert_called_once()
            self.assertIn('https://api.example/auth/link?t=', notify.call_args.args[1])
        with patch.dict(os.environ, {'DASHBOARD_LINK_SECRET': SECRET, 'DASHBOARD_API_BASE': 'https://api.example'}), \
                patch.object(app_module, 'find_client', return_value={**cfg, 'modules': {}}), \
                patch.object(app_module, 'detailed_report'), \
                patch.object(app_module, 'notify_owner') as notify:
            pipeline.process_line_event('dest', event)
            notify.assert_not_called()



class SyncPlanTests(unittest.TestCase):
    def test_rows_map_to_ledger_rows_with_event_keys_or_content_keys(self):
        from notimate.projections.sheets_sync import plan_tab
        expenses = plan_tab('Расходы', [
            {'Дата': '2026-09-20', 'Поставщик/Магазин': 'Makro', 'Позиция': 'milk', 'Сумма (THB)': '1,250฿', 'NotiMate Event ID': 'evt1:invoice-expense:0'},
            {'Дата': '2026-09-19', 'Поставщик/Магазин': 'Old', 'Позиция': 'x', 'Сумма (THB)': 40},
            {'Дата': '2026-09-19', 'Поставщик/Магазин': 'Old', 'Позиция': 'x', 'Сумма (THB)': 40},
            {'Дата': 'вчера', 'Сумма (THB)': 1},
        ], 'THB')
        self.assertEqual(len(expenses), 3)
        self.assertEqual((expenses[0]['key'], expenses[0]['values']['amount'], expenses[0]['event_keyed']), ('evt1:invoice-expense:0', 1250.0, True))
        self.assertTrue(expenses[1]['key'].startswith('sync:Расходы:') and expenses[1]['key'].endswith(':0'))
        self.assertTrue(expenses[2]['key'].endswith(':1'))  # identical hand-typed rows stay distinct
        self.assertNotEqual(expenses[1]['key'], expenses[2]['key'])

    def test_editing_a_handtyped_row_changes_its_key(self):
        from notimate.projections.sheets_sync import plan_tab
        row = {'Дата': '2026-09-19', 'Поставщик/Магазин': 'Old', 'Позиция': 'x', 'Сумма (THB)': 40}
        a = plan_tab('Расходы', [row], 'THB')[0]['key']
        b = plan_tab('Расходы', [{**row, 'Сумма (THB)': 45}], 'THB')[0]['key']
        self.assertNotEqual(a, b)

    def test_other_tabs_and_grouped_dates(self):
        from notimate.projections.sheets_sync import plan_tab
        revenue = plan_tab('Выручка', [{'Дата': '2026-09-20', 'Смена': '2', 'Gross Sales': '10,885', 'Наличные': 3440, 'Карта': 4750, 'QR': 2695}], 'THB')
        self.assertEqual(revenue[0]['values']['amount'], 10885.0)
        self.assertEqual(revenue[0]['values']['details'], {'cash': 3440, 'card': 4750, 'qr': 2695})
        grouped = plan_tab('Закупки', [{'Дата': '2026-09-20', 'Продукт': 'a', 'Количество': 1}, {'Дата': '', 'Продукт': 'b', 'Количество': 2}, {'Дата': '2026-09-21', 'Продукт': 'c', 'Количество': 3}], 'THB')
        self.assertEqual([g['values']['occurred_on'] for g in grouped], ['2026-09-20', '2026-09-20', '2026-09-21'])
        self.assertEqual(plan_tab('Остатки', [{'Дата': '2026-09-20', 'Продукт': 'Сыр', 'Холодильник': 3, 'Примечание': 'Low stock'}], 'THB')[0]['table'], 'stock_signals')
        self.assertEqual(plan_tab('Напоминания', [{'Название': 'Лицензия', 'Дата окончания': '2026-12-01'}], 'THB')[0]['table'], 'reminders')
        self.assertEqual(plan_tab('Проблемы', [{'Дата': '2026-09-20 10:15', 'Сообщение': 'сломался', 'Перевод и совет': 'вызвать'}], 'THB')[0]['table'], 'issues')
        self.assertEqual(plan_tab('Зарплаты', [{'Дата': '2026-09-20', 'Получатель': 'Ann', 'Сумма (THB)': 500}], 'THB')[0]['values']['operation_type'], 'salary')


if __name__ == '__main__':
    unittest.main()
