"""Management metadata for the existing controlled CLI binding; no credential copies."""
from datetime import date
from decimal import Decimal, InvalidOperation
import json

from .jobs import GatewayError, now


def registration(value):
    fields = {'membership_tier', 'membership_expiry', 'monthly_price_usd',
              'remaining_credits', 'monthly_credits', 'video_concurrency', 'image_concurrency', 'note'}
    if not isinstance(value, dict) or set(value) - fields:
        raise ValueError('invalid_registration')
    result = dict(value)
    for field in ('membership_tier', 'note'):
        if field in result and (not isinstance(result[field], str) or len(result[field]) > 2000):
            raise ValueError('invalid_registration')
    expiry = result.get('membership_expiry')
    if expiry is not None:
        if not isinstance(expiry, str):
            raise ValueError('invalid_expiry')
        if expiry:
            date.fromisoformat(expiry)
    for field in ('monthly_price_usd', 'remaining_credits', 'monthly_credits'):
        value = result.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or len(value) > 32:
            raise ValueError('invalid_amount')
        try:
            amount = Decimal(value)
            if not amount.is_finite() or amount < 0 or amount > Decimal('1e12') or amount.as_tuple().exponent < -6:
                raise ValueError('invalid_amount')
        except InvalidOperation:
            raise ValueError('invalid_amount') from None
        result[field] = format(amount, 'f')
    for field in ('video_concurrency', 'image_concurrency'):
        value = result.get(field)
        if value is not None and (type(value) is not int or not 0 <= value <= 10000):
            raise ValueError('invalid_concurrency')
    return result


class BoundAccount:
    def __init__(self, gateway):
        self.gateway = gateway
        with gateway.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS channel_account (id INTEGER PRIMARY KEY CHECK(id=1), record TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS channel_account_audit (id INTEGER PRIMARY KEY, record TEXT NOT NULL)')
            initial = {'schema_version': 1, 'account_id': gateway.binding, 'name': 'Higgsfield 账号',
                       'status': 'active', 'revision': 1, 'registration': {}, 'updated_at': now()}
            db.execute('INSERT OR IGNORE INTO channel_account VALUES(1,?)', (json.dumps(initial),))

    @staticmethod
    def read(db):
        row = db.execute('SELECT record FROM channel_account WHERE id=1').fetchone()
        if not row:
            raise GatewayError('account_unavailable', 503)
        return json.loads(row[0])

    def get(self):
        with self.gateway.db() as db:
            return self.read(db)

    def update(self, value, actor, *, archive=False):
        if not isinstance(value, dict) or set(value) - {'expected_revision', 'name', 'status', 'registration'}:
            raise ValueError('invalid_account_request')
        if type(value.get('expected_revision')) is not int:
            raise ValueError('invalid_revision')
        with self.gateway._lock, self.gateway.db() as db:
            previous = self.read(db)
            if previous['revision'] != value['expected_revision']:
                raise GatewayError('revision_conflict')
            if previous['status'] == 'archived':
                raise GatewayError('account_archived')
            status = 'archived' if archive else value.get('status', previous['status'])
            if status not in ({'archived'} if archive else {'active', 'disabled'}):
                raise ValueError('invalid_status')
            name = value.get('name', previous['name'])
            if not isinstance(name, str) or not name.strip() or len(name) > 200:
                raise ValueError('invalid_name')
            after = {**previous, 'status': status, 'name': name.strip(), 'revision': previous['revision'] + 1,
                     'registration': registration(value.get('registration', previous['registration'])), 'updated_at': now()}
            db.execute('UPDATE channel_account SET record=? WHERE id=1', (json.dumps(after),))
            db.execute('INSERT INTO channel_account_audit(record) VALUES(?)',
                       (json.dumps({'actor': actor, 'before': previous, 'after': after}),))
            return after
