"""Durable 48-hour fee deadlines and eight-hour reminders; no network on import."""
from datetime import datetime, timezone
from decimal import Decimal
import math
import core as c

PAYMENT_WINDOW = 48 * 3600
REMINDER_INTERVAL = 8 * 3600


def status(conn, uid, at=None):
    at = c.now() if at is None else at
    rows = conn.execute("SELECT fee,fee_pending_at FROM trades WHERE user_id=? AND fee_status='pending' AND status='closed'", (uid,)).fetchall()
    rows = [r for r in rows if Decimal(r['fee']) > 0]
    amount = sum((Decimal(r['fee']) for r in rows), Decimal(0))
    anchor = min(r['fee_pending_at'] for r in rows) if rows else None
    uncertain = bool(conn.execute("SELECT 1 FROM invoices WHERE user_id=? AND state IN ('submitting','uncertain')", (uid,)).fetchone())
    deadline = anchor + PAYMENT_WINDOW if anchor is not None else None
    return {
        'pendingAmount': format(amount, '.6f'),
        'firstPendingAt': anchor * 1000 if anchor is not None else None,
        'deadlineAt': deadline * 1000 if deadline is not None else None,
        'remainingSeconds': max(0, math.ceil(deadline - at)) if deadline is not None else None,
        'hold': uncertain,
        'overdue': bool(deadline is not None and at >= deadline),
        'serverTime': at * 1000,
        'paymentWindowHours': 48,
        'reminderHours': 8,
    }


def cancel_stale_reminders(conn, uid):
    """Use in the same transaction that confirms payment, under the user lock."""
    conn.execute("DELETE FROM outbox WHERE user_id=? AND kind='fee_reminder'", (uid,))
    current = status(conn, uid)
    if current['firstPendingAt'] is None:
        conn.execute('DELETE FROM fee_reminders WHERE user_id=?', (uid,))


def process_user(uid):
    """Serialize with payments, SMTP and account writes. Safe across restarts."""
    deleted = False
    with c.user_lock(uid), c.db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        u = conn.execute('SELECT deleting FROM users WHERE id=?', (uid,)).fetchone()
        if not u or u['deleting']:
            return False
        at = c.now()
        current = status(conn, uid, at)
        if current['firstPendingAt'] is None:
            cancel_stale_reminders(conn, uid)
            return False
        if current['hold']:
            # Never mistake a timeout, or a process crash during payment, for nonpayment.
            conn.execute("DELETE FROM outbox WHERE user_id=? AND kind='fee_reminder'", (uid,))
            return False
        if current['overdue']:
            deleted = c.erase_profile_locked(conn, uid)
        else:
            anchor = current['firstPendingAt'] / 1000
            slot = int((at - anchor) // REMINDER_INTERVAL)
            previous = conn.execute('SELECT * FROM fee_reminders WHERE user_id=?', (uid,)).fetchone()
            if previous is None or previous['anchor'] != anchor or slot > previous['last_slot']:
                # Keep only the current reminder after an outage; no backlog burst.
                conn.execute("DELETE FROM outbox WHERE user_id=? AND kind='fee_reminder'", (uid,))
                key = c.digest(f'fee-reminder:{uid}:{anchor}:{slot}')
                conn.execute("INSERT INTO outbox(id,user_id,subject,body,created,kind) VALUES(?,?,?,?,?,'fee_reminder')",
                             (key, uid, 'Comisiones pendientes / Outstanding fees', '', at))
                conn.execute('INSERT INTO fee_reminders(user_id,anchor,last_slot) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET anchor=excluded.anchor,last_slot=excluded.last_slot',
                             (uid, anchor, slot))
    if deleted:
        with c.db() as conn:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    return deleted


def reminder_body(conn, uid):
    """Build at send time so queued mail never reports a paid or outdated amount."""
    current = status(conn, uid)
    if current['firstPendingAt'] is None or current['hold'] or current['overdue']:
        return None
    deadline = datetime.fromtimestamp(current['deadlineAt'] / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    minutes = math.ceil(current['remainingSeconds'] / 60)
    remaining = f'{minutes // 60} h {minutes % 60:02d} min'
    amount = current['pendingAmount']
    return (
        f'Tienes {amount} USDC de comisiones pendientes en CryptoLove.\n'
        f'Paga antes de: {deadline}. Tiempo restante: {remaining}.\n'
        'Dispones de 48 horas (dos días) desde que se registra la primera comisión pendiente. '
        'Nuevas comisiones, reinicios o volver a iniciar sesión no amplían ese plazo.\n'
        'Si al vencer sigue pendiente alguna comisión vencida, se dará de baja tu cuenta '
        'y se borrarán tu perfil, credenciales, sesiones, historial privado y estado local del bot. '
        'La automatización se detendrá. Las posiciones abiertas y órdenes nativas permanecerán '
        'en Hyperliquid; tendrás que gestionarlas y revocar el agente allí. Los stops lógicos/HMA dejarán de gestionarse.\n'
        f'Entra en {c.FRONT}/#private y pulsa «Pagar comisiones pendientes». Confirma el pago en MetaMask.\n'
        'Recibirás recordatorios cada 8 horas hasta pagar o llegar al vencimiento.\n\n'
        f'You have {amount} USDC in outstanding CryptoLove fees.\n'
        f'Pay before: {deadline}. Time remaining: {remaining}.\n'
        'Payment is due within 48 hours (two days) of the oldest outstanding fee being recorded. '
        'New fees, restarts and signing in again do not extend that fee’s deadline.\n'
        'If an overdue fee remains unpaid, your account will be deleted, including your profile, '
        'credentials, sessions, private history and local bot state. Automation stops. Open positions '
        'and native orders remain on Hyperliquid; manage them and revoke the agent there. Logical/HMA stops will no longer be managed.\n'
        f'Visit {c.FRONT}/#private, choose “Pay pending fees” and confirm in MetaMask.\n'
        'Reminders are sent every 8 hours until payment or the deadline.\n'
        'Los correos ya enviados, las copias externas y los datos públicos de blockchain no se borran.\n'
        'Previously delivered emails, external backups and public blockchain records are not erased.'
    )
