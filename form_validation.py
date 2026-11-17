"""Form checks, without messages, mailbox probes or identity verification."""
import re
from datetime import date
from decimal import Decimal, InvalidOperation

try:
    import phonenumbers
except ImportError:
    phonenumbers = None
try:
    from email_validator import validate_email, EmailNotValidError, EmailUndeliverableError
except ImportError:
    validate_email = None


def check_value(value, kind, field):
    if len(value) > (10000 if kind == 'textarea' else 500):
        return 'invalid', 'Valore troppo lungo.'
    if any(ord(c) < 32 and c not in '\n\r\t' for c in value):
        return 'invalid', 'Il valore contiene caratteri di controllo.'
    if kind == 'email':
        if validate_email is None:
            if not re.fullmatch(r'[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+', value):
                return 'invalid', 'Formato email non valido.'
            return 'unknown', 'Controllo preliminare superato; verifica completa non disponibile (dipendenza email-validator mancante).'
        try:
            result = validate_email(value, check_deliverability=True, timeout=2)
        except EmailUndeliverableError:
            return 'invalid', 'Il dominio non risulta configurato per ricevere email.'
        except EmailNotValidError:
            return 'invalid', 'Formato email non valido.'
        if getattr(result, 'mx', None) is None:
            return 'unknown', 'Formato valido; controllo DNS non disponibile. Esistenza della casella non verificata.'
        return 'valid', 'Formato e dominio email validi. Esistenza e titolare della casella non verificati.'
    if kind == 'tel':
        if not re.fullmatch(r'\+?[0-9\s().-]+', value):
            return 'invalid', 'Usa cifre e un prefisso internazionale, ad esempio +39.'
        if phonenumbers is None:
            if not 7 <= len(re.sub(r'\D', '', value)) <= 15:
                return 'invalid', 'Lunghezza del numero non valida.'
            return 'unknown', 'Controllo preliminare superato; piano telefonico non verificato (dipendenza phonenumbers mancante).'
        try:
            number = phonenumbers.parse(value, 'IT')
            valid = phonenumbers.is_valid_number(number)
        except phonenumbers.NumberParseException:
            valid = False
        if not valid:
            return 'invalid', 'Prefisso o numero non valido. Senza prefisso internazionale viene usata Italia (+39).'
        return 'valid', 'Numero compatibile con il piano telefonico. Attivazione e titolare non verificati.'
    if kind == 'date':
        try:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError()
            date.fromisoformat(value)
        except ValueError:
            return 'invalid', 'Inserisci una data valida.'
    if kind == 'number':
        try:
            if not Decimal(value).is_finite():
                raise InvalidOperation()
        except InvalidOperation:
            return 'invalid', 'Inserisci un numero valido.'
    if kind == 'select' and value not in [s.strip() for s in field.get('options', '').split(',') if s.strip()]:
        return 'invalid', 'Scegli una delle opzioni disponibili.'
    return 'valid', 'Formato valido; contenuto e identita non verificati.'


def validate_form(fields, submitted):
    """Use the server-side schema, never field types supplied by the visitor."""
    checks, cleaned, errors = {}, {}, {}
    if not isinstance(submitted, dict):
        return {}, {}, {'form': 'Formato del modulo non valido.'}
    for field in fields:
        name = field.get('name', '')
        if not name:
            continue
        value = submitted.get(name, '')
        if not isinstance(value, str):
            state, message = 'invalid', 'Inserisci un valore testuale.'
        else:
            value = value.strip()
            if not value:
                state, message = ('invalid', 'Campo obbligatorio.') if field.get('required') else ('empty', 'Non compilato.')
            else:
                state, message = check_value(value, field.get('type', 'text'), field)
                cleaned[name] = value
        checks[name] = {'status': state, 'message': message}
        if state == 'invalid':
            errors[name] = str(field.get('label') or name) + ': ' + message
    return cleaned, checks, errors
