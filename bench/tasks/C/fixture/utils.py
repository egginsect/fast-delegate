"""Utility functions for common operations."""

import datetime


def chunk(items, size):
    """Break an iterable into chunks of specified size.

    Args:
        items: Iterable to chunk
        size: Size of each chunk

    Yields:
        Lists of items, each with at most 'size' elements
    """
    current_chunk = []
    for item in items:
        current_chunk.append(item)
        if len(current_chunk) == size:
            yield current_chunk
            current_chunk = []


def business_days_between(start_date, end_date):
    """Calculate the number of business days between two dates (exclusive).

    The start date is included but the end date is excluded.

    Args:
        start_date: datetime.date object (included in count)
        end_date: datetime.date object (excluded from count)

    Returns:
        Number of business days (Monday-Friday) in the range
    """
    count = 0
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            count += 1
        current += datetime.timedelta(days=1)
    return count


def weekday_name(date_obj):
    """Get the weekday name from a date object.

    Args:
        date_obj: A datetime.date object

    Returns:
        'weekend' or 'business_day'
    """
    if date_obj.weekday() < 6:
        return 'business_day'
    return 'weekend'


def parse_duration(duration_str):
    """Parse a duration string like '1h30m' into total seconds.

    Supports: h (hours), m (minutes), s (seconds).

    Args:
        duration_str: String like '1h30m' or '45s'

    Returns:
        Total seconds as an integer
    """
    total_seconds = 0
    i = 0
    current_number = ""

    while i < len(duration_str):
        if duration_str[i].isdigit():
            current_number += duration_str[i]
        elif duration_str[i] in 'hms':
            if not current_number:
                return 0

            value = int(current_number)
            if duration_str[i] == 'h':
                total_seconds += value * 3600
            elif duration_str[i] == 'm':
                total_seconds += value * 60
            elif duration_str[i] == 's':
                total_seconds += value
            current_number = ""
        else:
            return 0

        i += 1

    return total_seconds
