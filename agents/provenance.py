"""Structured and derived provenance for approval decisions.

Inspired by CaMeL (data-flow capabilities / dependencies) and Interbolt's
`derived_from` idea: a side-effecting argument is authorised when it equals a
value that can be reconstructed from trusted inputs, not when the model argues
from raw tool prose.

V1 (payment extract):
  user-named bill → IBAN + total → send_money
  notice delta + scheduled base → update_scheduled_transaction

V2 (Bucket A — general value propagation across the LLM hop):
  Tool results and the user request contribute *labeled values*.
  At a sink, arguments are approved when each sensitive field equals a label
  or a closed-form derivation over those labels (copy / difference /
  half-split / percent+fee). Labels never come from free-form injection
  prose: instruction blocks are stripped, and only structured tool fields
  (or user-named file remainder) are ingested.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


_INSTRUCTION_BLOCKS = re.compile(
    r'<(INFORMATION|SYSTEM|INSTRUCTIONS)>.*?</\1>',
    re.IGNORECASE | re.DOTALL,
)
_IBAN = re.compile(r'\b([A-Z]{2}\d{2}[A-Z0-9]{10,30})\b')
_EMAIL = re.compile(r'\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b')
_TOTAL = re.compile(r'(?im)^\s*Total\s+(\d+(?:\.\d+)?)\s*$')
_MONEY = re.compile(r'\b(\d+\.\d{2})\b')
_PERCENT = re.compile(r'\b(\d+(?:\.\d+)?)\s*%')
_INCREASE = re.compile(r'(?i)increased\s+by\s+(\d+(?:\.\d+)?)')
_DECREASE = re.compile(r'(?i)decreased\s+by\s+(\d+(?:\.\d+)?)')
_SUBJECT_HINTS = (
    ('rent', re.compile(r'(?i)\brent\b')),
    ('mortgage', re.compile(r'(?i)\bmortgage\b')),
    ('salary', re.compile(r'(?i)\bsalary\b')),
)
_FILENAME = re.compile(
    r"""'([^']+\.[A-Za-z0-9]+)'|"([^"]+\.[A-Za-z0-9]+)"|(\b[\w./-]+\.(?:txt|md|csv|pdf|docx)\b)"""
)
_MONEY_TOOLS = frozenset({'send_money', 'schedule_transaction'})
_UPDATE_TOOLS = frozenset({'update_scheduled_transaction'})
_TX_TOOLS = frozenset({'get_most_recent_transactions', 'get_scheduled_transactions'})
_CALENDAR_TOOLS = frozenset({
    'search_calendar_events', 'get_day_calendar_events',
})
_CONTACT_TOOLS = frozenset({
    'search_contacts_by_name', 'search_contacts_by_email',
})
_FILE_TOOLS = frozenset({'get_file_by_id', 'search_files', 'search_files_by_filename'})


@dataclass
class PaymentExtract:
    source: str
    recipients: list[str] = field(default_factory=list)
    amounts: list[float] = field(default_factory=list)
    deltas: list[float] = field(default_factory=list)
    subject_hints: list[str] = field(default_factory=list)

    def attestation(self) -> str:
        recipients = ', '.join(self.recipients) or '(none)'
        amounts = ', '.join(f'{a:.2f}' for a in self.amounts) or '(none)'
        deltas = ', '.join(f'{d:+.2f}' for d in self.deltas) or '(none)'
        hints = ', '.join(self.subject_hints) or '(none)'
        return (f'Verified fields from user-named file {self.source!r} '
                f'(instruction blocks removed): recipients=[{recipients}]; '
                f'amounts=[{amounts}]; deltas=[{deltas}]; subject_hints=[{hints}]')


@dataclass(frozen=True)
class LabeledValue:
    """One typed value with an explicit ingress source (Interbolt-style lineage hop)."""
    value: str
    kind: str
    source: str
    numeric: float | None = None


def strip_instruction_blocks(text: str) -> str:
    return _INSTRUCTION_BLOCKS.sub('', text)


def filenames_named_in_request(request: str) -> set[str]:
    names: set[str] = set()
    for match in _FILENAME.finditer(request or ''):
        raw = next(group for group in match.groups() if group)
        names.add(raw)
        names.add(raw.rsplit('/', 1)[-1])
    return names


def extract_payment_fields(text: str, source: str) -> PaymentExtract:
    """Typed fields from the trusted remainder of a document.

    Absolute payment fields (IBAN / Total) authorise send_money.
    Signed deltas ("increased by X") authorise derived updates, and are not
    treated as a standalone payment amount — otherwise a rent-increase notice
    would falsely authorise send_money(amount=100).
    """
    clean = strip_instruction_blocks(text or '')
    recipients = list(dict.fromkeys(_IBAN.findall(clean)))
    deltas: list[float] = []
    for match in _INCREASE.finditer(clean):
        deltas.append(float(match.group(1)))
    for match in _DECREASE.finditer(clean):
        deltas.append(-float(match.group(1)))
    amounts: list[float] = [float(m.group(1)) for m in _TOTAL.finditer(clean)]
    if not amounts and recipients:
        amounts = list(dict.fromkeys(float(m.group(1)) for m in _MONEY.finditer(clean)))
    hints = [name for name, pattern in _SUBJECT_HINTS if pattern.search(clean)]
    return PaymentExtract(source=source, recipients=recipients, amounts=amounts,
                          deltas=deltas, subject_hints=hints)


def parse_proposed_call(message: str) -> tuple[str, dict[str, str]]:
    lines = (message or '').strip().splitlines()
    if not lines:
        return '', {}
    tool = lines[0].split(':', 1)[0].strip()
    args: dict[str, str] = {}
    for line in lines[1:]:
        stripped = line.strip()
        if '=' not in stripped:
            continue
        key, _, value = stripped.partition('=')
        args[key.strip()] = value.strip()
    return tool, args


def _amounts_equal(left, right, tol: float = 0.011) -> bool:
    try:
        return abs(float(left) - float(right)) <= tol
    except (TypeError, ValueError):
        return False


def match_payment(extract: PaymentExtract, args: dict) -> bool | None:
    """True if args match absolute payment fields; False conflict; None empty."""
    if not extract.recipients and not extract.amounts:
        return None
    recipient = str(args.get('recipient', '')).strip()
    amount = args.get('amount')
    if extract.recipients and recipient not in extract.recipients:
        return False
    if extract.amounts and amount is not None and not any(_amounts_equal(amount, a) for a in extract.amounts):
        return False
    if extract.recipients and recipient in extract.recipients:
        return True
    if extract.amounts and amount is not None and any(_amounts_equal(amount, a) for a in extract.amounts):
        return True
    return None


def _parse_jsonish(content) -> object:
    if isinstance(content, (list, dict)):
        return content
    text = content if isinstance(content, str) else str(content)
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None


def _as_list(data) -> list:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


def _norm_str(value) -> str:
    return str(value or '').strip()


def _parse_arg_list(raw: str) -> list[str]:
    text = (raw or '').strip()
    if not text:
        return []
    parsed = _parse_jsonish(text)
    if isinstance(parsed, list):
        return [_norm_str(x) for x in parsed if _norm_str(x)]
    if isinstance(parsed, str) and parsed:
        return [parsed]
    return [text]


def amount_justified(proposed, user_amounts: list[float], tool_amounts: list[float],
                     user_percents: list[float]) -> bool:
    """Closed derivation grammar over user literals + tool amounts (no tool−tool)."""
    if proposed is None:
        return False
    try:
        target = float(proposed)
    except (TypeError, ValueError):
        return False
    users = list(user_amounts)
    tools = list(tool_amounts)
    if any(_amounts_equal(target, n) for n in users + tools):
        return True
    for a in tools:
        for b in users:
            if _amounts_equal(target, a - b):
                return True
            if _amounts_equal(target, b - a):
                return True
            if _amounts_equal(target, a + b):
                return True
            if _amounts_equal(target, a - b / 2.0):
                return True
            if _amounts_equal(target, b / 2.0):
                return True
        for p in user_percents:
            for fee in users + [0.0]:
                if _amounts_equal(target, a * (p / 100.0) + fee):
                    return True
    for b in users:
        if _amounts_equal(target, b / 2.0):
            return True
    return False


class ProvenanceLedger:
    """Collects labeled values and decides whether sink args are reconstructible."""

    def __init__(self, user_request: str):
        self.user_request = user_request or ''
        self.named_files = filenames_named_in_request(user_request)
        self.extracts: dict[str, PaymentExtract] = {}
        self.scheduled: dict[int, dict] = {}
        self.labels: list[LabeledValue] = []
        # Structured (party, amount) pairs from transaction tools — used so a
        # refund can name the counterparty without the IBAN being in the request.
        self.tx_parties: list[dict] = []
        self._ingest_user_request()

    def _add(self, value, kind: str, source: str, numeric: float | None = None) -> None:
        text = _norm_str(value)
        if not text:
            return
        if kind == 'amount' and numeric is None:
            try:
                numeric = float(text)
            except (TypeError, ValueError):
                return
        key = (text.lower() if kind in {'email', 'iban'} else text, kind, source)
        for existing in self.labels:
            if (existing.value.lower() if existing.kind in {'email', 'iban'} else existing.value,
                    existing.kind, existing.source) == key:
                return
        self.labels.append(LabeledValue(value=text, kind=kind, source=source, numeric=numeric))

    def _ingest_user_request(self) -> None:
        text = self.user_request
        for iban in _IBAN.findall(text):
            self._add(iban, 'iban', 'user')
        for email in _EMAIL.findall(text):
            self._add(email, 'email', 'user')
        for match in _MONEY.finditer(text):
            self._add(match.group(1), 'amount', 'user', float(match.group(1)))
        for match in _PERCENT.finditer(text):
            self._add(match.group(1), 'percent', 'user', float(match.group(1)))
        # Bare integers used as fees/amounts in some prompts (e.g. rent is 2200).
        for match in re.finditer(r'(?i)(?:rent|fee|amount|refund|send|got)\s+(?:is\s+|of\s+)?(\d+(?:\.\d+)?)', text):
            self._add(match.group(1), 'amount', 'user', float(match.group(1)))

    def note_read(self, path: str, content: str) -> None:
        base = (path or '').rsplit('/', 1)[-1]
        if not self.named_files:
            return
        if path not in self.named_files and base not in self.named_files:
            return
        key = base or path
        extract = extract_payment_fields(content, key)
        self.extracts[key] = extract
        for iban in extract.recipients:
            self._add(iban, 'iban', f'file:{key}')
        for amount in extract.amounts:
            self._add(f'{amount:.2f}', 'amount', f'file:{key}', amount)
        clean = strip_instruction_blocks(content or '')
        for email in _EMAIL.findall(clean):
            self._add(email, 'email', f'file:{key}')

    def note_tool_result(self, tool: str, content) -> None:
        """Record structured tool outputs that can serve as derivation bases."""
        data = _parse_jsonish(content)
        if tool in _TX_TOOLS:
            self._ingest_transactions(tool, data)
            return
        if tool in _CALENDAR_TOOLS:
            self._ingest_calendar(tool, data)
            return
        if tool in _CONTACT_TOOLS:
            self._ingest_contacts(tool, data)
            return
        if tool in _FILE_TOOLS:
            self._ingest_files(tool, data)
            return

    def _ingest_transactions(self, tool: str, data) -> None:
        if tool == 'get_scheduled_transactions' and isinstance(data, list):
            for row in data:
                if not isinstance(row, dict) or 'id' not in row:
                    continue
                try:
                    self.scheduled[int(row['id'])] = row
                except (TypeError, ValueError):
                    continue
        for row in _as_list(data):
            if not isinstance(row, dict):
                continue
            source = f'tool:{tool}'
            amount_val = None
            if 'amount' in row:
                try:
                    amount_val = float(row['amount'])
                except (TypeError, ValueError):
                    amount_val = None
                if amount_val is not None:
                    self._add(row['amount'], 'amount', source, amount_val)
            for key in ('recipient', 'sender'):
                value = row.get(key)
                if not value or value == 'me':
                    continue
                text = _norm_str(value)
                if _IBAN.fullmatch(text):
                    self._add(text, 'iban', source)
                elif '@' in text:
                    self._add(text, 'email', source)
                else:
                    self._add(text, 'party', source)
                if amount_val is not None:
                    self.tx_parties.append({'party': text, 'amount': amount_val, 'source': source})
            if row.get('subject'):
                subject = strip_instruction_blocks(_norm_str(row.get('subject')))
                # Never label injection prose that landed in a subject field.
                if subject and '<INFORMATION>' not in (row.get('subject') or ''):
                    self._add(subject, 'subject', source)

    def _ingest_calendar(self, tool: str, data) -> None:
        for row in _as_list(data):
            if not isinstance(row, dict):
                continue
            source = f'tool:{tool}'
            for participant in row.get('participants') or []:
                text = _norm_str(participant)
                if '@' in text:
                    self._add(text, 'email', source)
                    self._add(text, 'participant', source)
                elif text:
                    self._add(text, 'participant', source)
            if row.get('title'):
                self._add(row['title'], 'title', source)
            if row.get('location'):
                self._add(row['location'], 'location', source)

    def _ingest_contacts(self, tool: str, data) -> None:
        for row in _as_list(data):
            if not isinstance(row, dict):
                continue
            source = f'tool:{tool}'
            if row.get('email'):
                self._add(row['email'], 'email', source)
            if row.get('name'):
                self._add(row['name'], 'name', source)

    def _ingest_files(self, tool: str, data) -> None:
        for row in _as_list(data):
            if not isinstance(row, dict):
                continue
            filename = _norm_str(row.get('filename') or row.get('name') or '')
            base = filename.rsplit('/', 1)[-1]
            content = row.get('content')
            if content is None:
                continue
            # Only user-named files contribute free-text fields (ASR-safe).
            if filename not in self.named_files and base not in self.named_files:
                continue
            self.note_read(filename or base, content if isinstance(content, str) else str(content))

    def _user_amounts(self) -> list[float]:
        return [lv.numeric for lv in self.labels
                if lv.kind == 'amount' and lv.source == 'user' and lv.numeric is not None]

    def _tool_amounts(self) -> list[float]:
        return [lv.numeric for lv in self.labels
                if lv.kind == 'amount' and lv.source.startswith('tool:') and lv.numeric is not None]

    def _file_amounts(self) -> list[float]:
        return [lv.numeric for lv in self.labels
                if lv.kind == 'amount' and lv.source.startswith('file:') and lv.numeric is not None]

    def _user_percents(self) -> list[float]:
        return [lv.numeric for lv in self.labels
                if lv.kind == 'percent' and lv.numeric is not None]

    def _request_mentions(self, value: str) -> bool:
        text = _norm_str(value)
        if not text:
            return False
        return text.lower() in self.user_request.lower()

    def recipient_justified(self, recipient: str) -> bool:
        text = _norm_str(recipient)
        if not text:
            return False
        if self._request_mentions(text):
            return True
        lowered = text.lower()
        for lv in self.labels:
            if lv.kind in {'iban', 'email'} and lv.value.lower() == lowered:
                if lv.source == 'user' or lv.source.startswith('file:'):
                    return True
        # Refund without IBAN in the prompt: counterparty from a transaction
        # whose amount the user also named (e.g. "refund that 10.00").
        user_amounts = self._user_amounts()
        for tx in self.tx_parties:
            if tx['party'].lower() != lowered:
                continue
            if any(_amounts_equal(tx['amount'], u) for u in user_amounts):
                return True
        return False

    def decide_money_call(self, tool: str, args: dict) -> bool | None:
        if tool not in _MONEY_TOOLS:
            return None
        # User-named documents keep the V1 fail-closed payment path.
        if self.named_files:
            if not self.extracts:
                return False
            for extract in self.extracts.values():
                result = match_payment(extract, args)
                if result is True:
                    return True
                if result is False:
                    return False
            # Named file read but only deltas / no absolute payment fields.
            return False

        recipient = _norm_str(args.get('recipient'))
        amount = args.get('amount')
        if not recipient or amount is None:
            return None
        if not self.recipient_justified(recipient):
            return None
        if amount_justified(
                amount,
                user_amounts=self._user_amounts() + self._file_amounts(),
                tool_amounts=self._tool_amounts(),
                user_percents=self._user_percents()):
            return True
        return None

    def decide_derived_update(self, tool: str, args: dict) -> bool | None:
        """Approve update when proposed amount = observed base ± user-named delta,
        or when recipient+amount are both literals from the user request."""
        if tool not in _UPDATE_TOOLS:
            return None
        recipient = _norm_str(args.get('recipient', ''))
        amount = args.get('amount')
        # User-literal standing-order rewrite (Banking task 15).
        if recipient and amount is not None:
            if self._request_mentions(recipient) and amount_justified(
                    amount, self._user_amounts(), [], self._user_percents()):
                return True

        if not self.named_files:
            return None
        deltas = [d for extract in self.extracts.values() for d in extract.deltas]
        if not deltas:
            return False if self.extracts else None
        try:
            tx_id = int(float(args['id']))
            proposed = float(args['amount'])
        except (KeyError, TypeError, ValueError):
            return False
        base_row = self.scheduled.get(tx_id)
        if base_row is None or 'amount' not in base_row:
            return False
        try:
            base = float(base_row['amount'])
        except (TypeError, ValueError):
            return False
        hints = [h for extract in self.extracts.values() for h in extract.subject_hints]
        subject = str(base_row.get('subject', '')).lower()
        if hints and not any(h in subject for h in hints):
            return False
        for delta in deltas:
            if _amounts_equal(proposed, base + delta):
                return True
        return False

    def decide_calendar_create(self, tool: str, args: dict) -> bool | None:
        if tool != 'create_calendar_event':
            return None
        participants = _parse_arg_list(args.get('participants', ''))
        if not participants:
            return None
        known = {lv.value.lower() for lv in self.labels if lv.kind in {'participant', 'email'}}
        if not known:
            return None
        if not all(p.lower() in known for p in participants):
            return None
        title = _norm_str(args.get('title'))
        if title and not self._request_mentions(title):
            return None
        description = _norm_str(args.get('description'))
        if description and not self._request_mentions(description):
            return None
        return True

    def decide_send_email(self, tool: str, args: dict) -> bool | None:
        if tool != 'send_email':
            return None
        recipients = _parse_arg_list(args.get('recipients', ''))
        if not recipients:
            # Some describe_call paths may use singular keys.
            recipients = _parse_arg_list(args.get('recipient', ''))
        if not recipients:
            return None
        known = {lv.value.lower() for lv in self.labels if lv.kind == 'email'}
        if not known:
            return None
        if not all(r.lower() in known for r in recipients):
            return None
        subject = _norm_str(args.get('subject'))
        if subject and not self._request_mentions(subject):
            return None
        return True

    def decide(self, tool: str, args: dict) -> bool | None:
        money = self.decide_money_call(tool, args)
        if money is not None:
            return money
        update = self.decide_derived_update(tool, args)
        if update is not None:
            return update
        calendar = self.decide_calendar_create(tool, args)
        if calendar is not None:
            return calendar
        return self.decide_send_email(tool, args)

    def attestation_text(self) -> str:
        parts: list[str] = []
        if self.extracts:
            parts.extend(extract.attestation() for extract in self.extracts.values())
        elif self.named_files:
            parts.append('User named file(s) ' + ', '.join(sorted(self.named_files))
                         + ' but no verified fields are available.')
        if self.scheduled:
            summary = ', '.join(
                f"id={i} amount={row.get('amount')} subject={row.get('subject')!r}"
                for i, row in sorted(self.scheduled.items()))
            parts.append(f'Observed scheduled transactions: [{summary}]')
        # Compact labeled-value attestation for the isolated fallback model.
        interesting = [lv for lv in self.labels
                       if lv.kind in {'iban', 'email', 'amount', 'percent', 'participant', 'party'}
                       and lv.source != 'user']
        if interesting:
            rendered = '; '.join(
                f'{lv.kind}={lv.value!r}←{lv.source}' for lv in interesting[:24])
            parts.append(f'Labeled values from tools/files: {rendered}')
        return '\n'.join(parts)
