"""Registration of the single ranked agent read."""
from elbow_helper.agent.reads.snapshots import family_tool, IDS, TAGS, DATE
from .projections import COHORT_REF
from .ranked import read_ranked, SELECTOR_ERROR
from .ranked_measures import METRICS, MODES

TOOLS = (family_tool('read_ranked_activity','Read collected ranked activity, complete account cohorts and collection gaps.',
    {'member_ids':IDS,'account_tags':TAGS,'clan_tags':TAGS,'after':DATE,'before':DATE,
     'membership_basis': {'type': 'string', 'enum': ['community', 'current_in_game']},
     'season': {'type': 'string', 'maxLength': 20},
     'include_battles':{'type':'boolean'}, 'detail': {'type': 'string', 'enum': ['summary', 'detail']},
     'metric': {'type': 'string', 'enum': list(METRICS)},
     'direction': {'type': 'string', 'enum': ['attacks', 'defenses']},
     'mode': {'type': 'string', 'enum': list(MODES)}, 'cohort_ref':COHORT_REF},read_ranked, required=('after',),
    rules=({'needs': ['member_ids', 'account_tags', 'clan_tags', 'cohort_ref'], 'error': SELECTOR_ERROR},
           {'only_one': ['member_ids', 'account_tags', 'clan_tags'], 'error': SELECTOR_ERROR},
           {'with': ['cohort_ref'], 'forbids': ['member_ids'], 'error': SELECTOR_ERROR},
           {'with': ['metric'], 'needs': ['direction'], 'error': 'Choose a supported battle measure.'}),
    usage_note='Choose metric, direction and mode for numbers. detail retains battle evidence. Counts include losses; gaps stay unknown. UTC, end exclusive; no live gap fill.'),)
