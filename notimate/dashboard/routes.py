"""Flask routes for the owner dashboard API and month-package downloads (Этап 8, docs/21).

Security model:
* Login links are HMAC-signed and live 5 minutes; opening one exchanges it for an HttpOnly,
  Secure, SameSite=Lax session cookie (12 h). No token ever appears in an API URL.
* Every API request re-reads the tenant and re-checks that the session's subject is still an
  owner — the tenant comes only from the verified session, never from query/body input, so
  one tenant can not name another tenant's data.
* Responses carry ``Cache-Control: no-store``; CORS is limited to the single configured
  dashboard origin (``DASHBOARD_ORIGIN``) with credentials.
* The whole feature stays off (404) until ``DASHBOARD_LINK_SECRET`` is set in the environment.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from typing import Any

from flask import Response, abort, jsonify, redirect, request

from notimate.dashboard import links
from notimate.dashboard.reader import build_snapshot
from notimate.timeutil import local_now

COOKIE = 'nm_session'
PERIOD_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')


def _secret() -> str:
    return os.environ.get('DASHBOARD_LINK_SECRET', '')


def _api_base() -> str:
    return os.environ.get('DASHBOARD_API_BASE', '')


def feature_enabled() -> bool:
    return bool(_secret() and _api_base())


def _no_store(response: Response) -> Response:
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Pragma'] = 'no-cache'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def link_enabled_for(tenant: dict[str, Any]) -> bool:
    """Which tenants may be issued links: the packs that have dashboard data by design, or
    any tenant that opts in with ``modules.dashboard.enabled`` (JSC stays off by default)."""
    if not feature_enabled():
        return False
    modules = tenant.get('modules') or {}
    dashboard = modules.get('dashboard') if isinstance(modules, dict) else None
    return tenant.get('vertical_pack') in ('location_reports', 'accountant') or bool(isinstance(dashboard, dict) and dashboard.get('enabled'))


def owner_link(tenant: dict[str, Any], subject: str) -> str | None:
    return links.build_login_url(_api_base(), _secret(), tenant['id'], subject) if link_enabled_for(tenant) else None


def package_link(tenant: dict[str, Any], subject: str, period: str) -> str | None:
    return links.build_package_url(_api_base(), _secret(), tenant['id'], subject, period) if feature_enabled() else None


def _authorized_tenant(claims: dict[str, Any], *, allow_accountant: bool = False) -> dict[str, Any] | None:
    import app
    if not app.tenant_store or not app.TENANTS_DB_ENABLED:
        return None
    try:
        tenant = app.tenant_store.get_tenant(claims['t'])
    except Exception:
        return None
    if not tenant:
        return None
    allowed = set(tenant['owner_ids'])
    if allow_accountant:
        accountant = (tenant.get('modules') or {}).get('accountant') if isinstance(tenant.get('modules'), dict) else None
        allowed |= {str(a) for a in (accountant or {}).get('accountant_ids', [])} if isinstance(accountant, dict) else set()
    return tenant if claims['s'] in allowed else None


def _session_claims() -> dict[str, Any] | None:
    return links.verify_token(_secret(), request.cookies.get(COOKIE, ''), 'session')


def register_routes(flask_app) -> None:
    @flask_app.after_request
    def cors(response):
        origin = os.environ.get('DASHBOARD_ORIGIN', '')
        if origin and request.headers.get('Origin') == origin and request.path.startswith('/v1/'):
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Access-Control-Allow-Credentials'] = 'true'
            response.headers['Vary'] = 'Origin'
        return response

    @flask_app.route('/auth/link', methods=['GET'])
    def dashboard_auth_link():
        if not feature_enabled():
            abort(404)
        claims = links.verify_token(_secret(), request.args.get('t', ''), 'link')
        if not claims or not _authorized_tenant(claims):
            return _no_store(Response('Ссылка недействительна или устарела. Запросите новую в чате.', status=403, mimetype='text/plain'))
        session = links.issue_token(_secret(), claims['t'], claims['s'], 'session', links.SESSION_TTL)
        target = os.environ.get('DASHBOARD_BASE_URL', '')
        response = redirect(target, code=302) if target else Response('OK', mimetype='text/plain')
        response.set_cookie(
            COOKIE, session, max_age=links.SESSION_TTL, httponly=True, samesite='Lax',
            secure=os.environ.get('DASHBOARD_INSECURE_COOKIES') != '1',
            domain=os.environ.get('DASHBOARD_COOKIE_DOMAIN') or None,
        )
        return _no_store(response)

    @flask_app.route('/v1/owner-dashboard', methods=['GET'])
    def owner_dashboard():
        import app
        if not feature_enabled():
            abort(404)
        claims = _session_claims()
        tenant = _authorized_tenant(claims) if claims else None
        if not tenant:
            return _no_store(jsonify({'error': 'unauthorized'})), 401
        period = request.args.get('period', 'day')
        if period not in ('day', 'week', 'month'):
            return _no_store(jsonify({'error': 'invalid_period'})), 400
        reader = getattr(app, 'dashboard_reader', None)
        if reader is None:
            return _no_store(jsonify({'error': 'unavailable'})), 503
        try:
            snapshot = build_snapshot(reader, tenant, period, local_now(tenant.get('timezone')))
        except Exception as exc:
            from logging_utils import get_logger
            get_logger().error('dashboard_snapshot_failed', extra={'error_type': type(exc).__name__})
            return _no_store(jsonify({'error': 'unavailable'})), 503
        return _no_store(jsonify(snapshot))

    @flask_app.route('/v1/owner-dashboard/logout', methods=['POST'])
    def owner_dashboard_logout():
        response = jsonify({'status': 'ok'})
        response.delete_cookie(COOKIE, domain=os.environ.get('DASHBOARD_COOKIE_DOMAIN') or None)
        return _no_store(response)

    @flask_app.route('/d/package', methods=['GET'])
    def download_package():
        import app
        if not feature_enabled():
            abort(404)
        claims = links.verify_token(_secret(), request.args.get('t', ''), 'package')
        period = (claims or {}).get('p', '')
        tenant = _authorized_tenant(claims, allow_accountant=True) if claims else None
        if not tenant or not PERIOD_RE.match(period):
            return _no_store(Response('Ссылка недействительна или устарела.', status=403, mimetype='text/plain'))
        store = getattr(app, 'documents_store', None)
        if not store or not app.DOCUMENTS_DB_ENABLED:
            return _no_store(Response('Модуль документов недоступен.', status=503, mimetype='text/plain'))
        from notimate.packs.accountant.flow import build_package_for
        data, _ = build_package_for(store, tenant, period)
        response = Response(data, mimetype='application/zip')
        response.headers['Content-Disposition'] = f"attachment; filename=\"{re.sub(r'[^A-Za-z0-9_-]', '_', tenant['id'])}-{period}.zip\""
        return _no_store(response)
