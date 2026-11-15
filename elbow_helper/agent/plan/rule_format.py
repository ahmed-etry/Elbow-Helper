"""Compact catalogue notation compiled from the enforced input predicates."""

from collections import Counter, defaultdict
from dataclasses import dataclass

from .input_rules import RULES


LEGEND = ('Rules: @ without report_id; -> implies; + all required; ? any required; '
          '^ at most one; ! none; | alternatives; & all; ~ absent; [] each item.')


@dataclass(frozen=True)
class RuleClause:
    source_rules: tuple[int, ...]
    condition: str
    effects: tuple[str, ...]

    def text(self):
        return (self.condition + '->' if self.condition else '') + ','.join(self.effects)


def _values(values):
    return '|'.join('~' if value is None else str(value).lower() if isinstance(value, bool)
                    else str(value) for value in values)


def _fields(operator, fields):
    fields = sorted(fields)
    if len(fields) == 1 and operator in ('+', '?', '!'):
        return ('+' if operator == '?' else operator) + fields[0]
    return operator + '(' + _values(fields) + ')'


def _match(field, allowed, detail, *, absent=True):
    text = field + '=' + _values(allowed)
    if 'enum' in detail:
        domain = [*detail['enum'], *([None] if absent else [])]
        excluded = [value for value in domain if value not in allowed]
        complement = field + '!=' + _values(excluded)
        if excluded and len(complement) < len(text):
            text = complement
    return text


def rule_clauses(schema):
    """Return clauses, type-covered rule indexes, and the shared report guard."""
    properties = schema.get('properties', {})
    rules = schema.get(RULES, ())
    guard = bool(rules) and 'report_id' in properties and all(
        'report_id' in rule.get('unless', ()) for rule in rules)
    groups, covered = {}, set()
    supported = {'when', 'with', 'unless', 'needs', 'needs_all', 'only_one',
                 'forbids', 'within', 'aware', 'error'}
    for index, rule in enumerate(rules):
        if unknown := set(rule) - supported:
            raise ValueError('Unrendered input rule kind: ' + ','.join(sorted(unknown)))
        effects = []
        for key, operator in (('needs', '?'), ('needs_all', '+'), ('only_one', '^'), ('forbids', '!')):
            if key in rule:
                effects.append(_fields(operator, rule[key]))
        for field, allowed in rule.get('within', {}).items():
            detail = properties.get(field, {}).get('items', {})
            if 'enum' not in detail or not set(detail['enum']) <= set(allowed):
                effects.append(_match(field + '[]', allowed, detail, absent=False))
        aware = [field for field in rule.get('aware', ())
                 if properties.get(field, {}).get('format') != 'date-time']
        if aware:
            effects.append('timezone(' + _values(aware) + ')')
        if not effects:
            covered.add(index)
            continue
        when = rule.get('when', {})
        given = tuple(rule.get('with', ()))
        missing = tuple(field for field in rule.get('unless', ()) if not guard or field != 'report_id')
        # Equal consequences with alternative values of one selector are one implication.
        field = next(iter(when)) if len(when) == 1 else None
        key = (field, given, missing, tuple(effects),
               tuple((name, tuple(values)) for name, values in when.items()) if field is None else ())
        if key not in groups:
            groups[key] = ([], {name: list(values) for name, values in when.items()})
        indices, combined = groups[key]
        indices.append(index)
        if field is not None:
            combined[field] = list(dict.fromkeys([*combined[field], *when[field]]))
    clauses = []
    for (field, given, missing, effects, _), (indices, when) in groups.items():
        conditions = [_match(name, values, properties.get(name, {})) for name, values in when.items()]
        conditions.extend(given)
        conditions.extend('!' + name for name in missing)
        clauses.append(RuleClause(tuple(indices), '&'.join(conditions), effects))
    return clauses, covered, guard


def catalogue_rules(registry):
    """Share repeated clauses in one legend; retain each lookup's conditions."""
    compiled = {name: rule_clauses(tool.definition.parameters) for name, tool in sorted(registry.items())}
    counts = Counter(clause.text() for clauses, _, _ in compiled.values() for clause in clauses)
    shared = {}
    for clause, count in counts.items():
        alias = 'r' + str(len(shared) + 1)
        if count > 1 and count * (len(clause) - len(alias)) > len(alias) + len(clause) + 2:
            shared[clause] = alias
    lines = {}
    for name, (clauses, _, guard) in compiled.items():
        if not clauses:
            continue
        references, groups = [], defaultdict(list)
        for clause in clauses:
            if clause.text() in shared:
                references.append(shared[clause.text()])
            else:
                groups[clause.condition].extend(clause.effects)
        local = [(condition + '->' if condition else '') + ','.join(effects)
                 for condition, effects in groups.items()]
        lines[name] = 'rules' + ('@ ' if guard else ' ') + ';'.join([*references, *local])
    legend = LEGEND if lines else ''
    if shared:
        legend += ' ' + ';'.join(alias + '=' + clause for clause, alias in shared.items())
    return legend, lines
