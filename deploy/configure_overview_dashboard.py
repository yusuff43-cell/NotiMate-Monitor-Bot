"""Create the native Google Sheets charts for the NotiMate owner dashboard.

Uses the service account already configured for the bot. It neither prints source
rows nor changes any operational worksheet.
"""
import json
import os

import gspread
from google.oauth2.service_account import Credentials


def grid_range(sheet_id, row_start, row_end, column_start, column_end):
    return {
        'sheetId': sheet_id,
        'startRowIndex': row_start,
        'endRowIndex': row_end,
        'startColumnIndex': column_start,
        'endColumnIndex': column_end,
    }


def chart(title, chart_type, sheet_id, domain, series, anchor_column):
    return {
        'addChart': {
            'chart': {
                'spec': {
                    'title': title,
                    'basicChart': {
                        'chartType': chart_type,
                        'legendPosition': 'BOTTOM_LEGEND',
                        'headerCount': 1,
                        'domains': [{'domain': {'sourceRange': {'sources': [domain]}}}],
                        'series': [
                            {'series': {'sourceRange': {'sources': [source]}}, 'targetAxis': 'LEFT_AXIS'}
                            for source in series
                        ],
                    },
                },
                'position': {
                    'overlayPosition': {
                        'anchorCell': {'sheetId': sheet_id, 'rowIndex': 11, 'columnIndex': anchor_column},
                        'widthPixels': 430,
                        'heightPixels': 250,
                    },
                },
            },
        },
    }


def main():
    clients = json.loads(os.environ['CLIENTS_JSON'])
    client_cfg = next(iter(clients.values()))
    credentials = Credentials.from_service_account_info(
        json.loads(os.environ['GOOGLE_CREDENTIALS']),
        scopes=['https://www.googleapis.com/auth/spreadsheets'],
    )
    sh = gspread.authorize(credentials).open_by_key(client_cfg['sheet_id'])
    metadata = sh.fetch_sheet_metadata()
    overview = next(sheet for sheet in metadata['sheets'] if sheet['properties']['title'] == 'Обзор')
    sheet_id = overview['properties']['sheetId']

    requests = [
        {'deleteEmbeddedObject': {'objectId': item['chartId']}}
        for item in overview.get('charts', [])
    ]
    requests.extend([
        {
            'updateDimensionProperties': {
                'range': {'sheetId': sheet_id, 'dimension': 'COLUMNS', 'startIndex': 0, 'endIndex': 8},
                'properties': {'pixelSize': 125},
                'fields': 'pixelSize',
            },
        },
        {
            'updateDimensionProperties': {
                'range': {'sheetId': sheet_id, 'dimension': 'COLUMNS', 'startIndex': 9, 'endIndex': 12},
                'properties': {'hiddenByUser': True},
                'fields': 'hiddenByUser',
            },
        },
        {
            'updateSheetProperties': {
                'properties': {'sheetId': sheet_id, 'gridProperties': {'hideGridlines': True}},
                'fields': 'gridProperties.hideGridlines',
            },
        },
        chart(
            'Выручка и расходы за 14 дней', 'LINE', sheet_id,
            grid_range(sheet_id, 1, 15, 9, 10),
            [grid_range(sheet_id, 1, 15, 10, 11), grid_range(sheet_id, 1, 15, 11, 12)], 0,
        ),
        chart(
            'Расходы по поставщикам за месяц', 'COLUMN', sheet_id,
            grid_range(sheet_id, 20, 27, 9, 10),
            [grid_range(sheet_id, 20, 27, 10, 11)], 4,
        ),
    ])
    sh.batch_update({'requests': requests})
    print({'dashboard_configured': True, 'charts': 2, 'helper_columns_hidden': True})


if __name__ == '__main__':
    main()
