from decimal import Decimal, InvalidOperation

from django import template


register = template.Library()


@register.filter
def dot_thousands(value):
    """Format a numeric value with Serbian-style dot thousands separators."""
    if value in (None, ""):
        return "–"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return value
    return f"{number:,.0f}".replace(",", ".")


@register.filter
def signed_dot_thousands(value):
    if value is None:
        return "–"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return value
    sign = "+" if number > 0 else ""
    return f"{sign}{number:,.0f}".replace(",", ".")
