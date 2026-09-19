#!/usr/bin/env python3
"""Print safe daily OpenAI usage aggregates from the Monitor Bot database."""

from __future__ import annotations

import argparse
import os

import psycopg


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=31, help='Bangkok calendar days to include (default: 31)')
    args = parser.parse_args()
    if args.days < 1 or args.days > 366:
        parser.error('--days must be between 1 and 366')
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        parser.error('DATABASE_URL is required')

    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT usage_date, model, requests, input_tokens, output_tokens, reasoning_tokens
            FROM openai_usage_daily
            WHERE usage_date >= ((NOW() AT TIME ZONE 'Asia/Bangkok')::date - (%s - 1))
            ORDER BY usage_date DESC, model
            """,
            (args.days,),
        ).fetchall()

    print('date\tmodel\trequests\tinput_tokens\toutput_tokens\treasoning_tokens')
    for row in rows:
        print('\t'.join(str(value) for value in row))


if __name__ == '__main__':
    main()
