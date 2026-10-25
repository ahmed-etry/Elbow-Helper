"""Sourced descriptive facts, independent of lookup and action permissions."""
from elbow_helper.configuration.clans import CLANS, CLAN_ORDER
from elbow_helper.features.clan_health.config import CLAN_PROFILE_BY_CODE


APPROVAL = 'STRUCTURAL-1-P2'
CLAN_SOURCE = 'elbow_helper/configuration/clans.py'
BRANCH_SOURCE = 'elbow_helper/features/clan_health/config.py'
PURPOSES = {
    'BEH':'Top competitive war environment',
    'BE4':'Main CWL clan and progression toward BEH',
    'BES':'Competitive entry and development',
    'BE1':'Casual play, competitive during CWL',
    'BEM':'Relaxed casual play',
    'BEC':'Development for lower-level, new and returning accounts',
    'BEE':'Development toward casual or competitive play',
    'BEP':'Utility clan',
    'BEW':'Extra Wars and friendly clashes',
}
PROFILE_LINES = {'BEH': 31, 'BE4': 39, 'BES': 47, 'BE1': 57, 'BEM': 69,
                 'BEC': 79, 'BEE': 87, 'BEP': 95, 'BEW': 103}


def clan_profile(code):
    clan = CLANS[code]
    return {'clan_code':code,'name':clan.name,'tag':clan.tag,
        'branch':CLAN_PROFILE_BY_CODE[code], 'purpose':PURPOSES[code], 'is_utility':clan.is_utility,
        'configured_cwl_only':clan.cwl_only, 'home_placement_eligible': not clan.is_utility,
        'home_placement_eligibility_basis': 'configured non-utility clan; not confirmation of active operation or admissions',
        'operational_status': None,
        'operational_cwl_only':None if code=='BEP' else clan.cwl_only,
        'has_placeholders':True if code=='BE4' else None,
        'membership_basis':None if code=='BEP' else 'seasonal_cwl_cohort' if clan.cwl_only else 'current_in_game',
        'configured_membership_basis':'seasonal_cwl_cohort' if clan.cwl_only else 'current_in_game',
        'operational_policy_status':'open_owner_question' if code=='BEP' else 'recorded',
        'town_hall_min':14 if code=='BES' else None,'town_hall_max':None,
        'town_hall_range_status':'approved_lower_bound_only' if code=='BES' else 'unknown',
        'sources':{'identity':CLAN_SOURCE,'branch':BRANCH_SOURCE,'configured_cwl_only':CLAN_SOURCE,
            'purpose':{'source':'K/clan-profiles.md', 'line': PROFILE_LINES[code],
                       'visibility':'public','approved_revision':APPROVAL},
            'has_placeholders':{'source': 'elbow_helper/features/agent/datasets/clash.py',
                                'authority': APPROVAL} if code=='BE4' else None,
            'town_hall_min':{'source':'K/clan-profiles.md', 'line': 53, 'visibility':'public',
                             'approved_revision':APPROVAL} if code=='BES' else None},
        'unknown_policy':'Null means unknown, not false or unrestricted. Current roster TH does not establish policy. '
            'Configured flags describe implementation choices, not confirmed operational policy. '
            'An unknown operational fact cannot be filled from a previous assistant statement.'}


def role_semantics():
    return [
        {'subject':'Core','holds':['Lead','Elder','Member'],
         'authority':'owner_supplied','approved_revision':APPROVAL},
        {'subject':'Leader BEx','holds':['Lead Plus'],
         'lead_plus_alternatives':['Lead','Co Applicant only'],
         'authority':'owner_supplied','approved_revision':APPROVAL},
    ]


def workflow_meanings():
    return [
        {'event':'account_linked','establishes':'account identity link',
         'does_not_establish':['submitted application','recruitment acceptance'],
         'source':'elbow_helper/features/agent/capabilities/account_links/reads.py'},
        {'event':'accept','establishes':'trial started',
         'does_not_establish':['permanent membership','historical absence when no active trial'],
         'source':'elbow_helper/features/recruitment/trials.py'},
        {'event':'trial_due','establishes':'expected trial end reached',
         'does_not_establish':['declined','finalized'],
         'source':'elbow_helper/features/recruitment/queries.py'},
        {'event':'trial_ended_followup_sent','establishes':'trial ended and follow-up recorded',
         'does_not_establish':['later acceptance','decline','finalization'],
         'source':'elbow_helper/features/agent/capabilities/recruitment/data.py'},
        {'event':'applicant_response_reminder','establishes':'response follow-up',
         'does_not_establish':['acceptance','placement','trial end'],
         'source':'elbow_helper/features/agent/capabilities/recruitment/data.py'},
        {'event':'examiner_routed','establishes':'examiner assignment',
         'does_not_establish':['recorded examination response','promotion decision'],
         'source':'elbow_helper/features/agent/capabilities/examination/data.py'},
        {'event':'hibernating','establishes':'active workflow record',
         'does_not_establish':['known exact start timestamp','observed inactivity'],
         'source':'elbow_helper/features/hibernation/queries.py'},
    ]


def domain_reference(kind, clan_codes=None):
    rows = ([clan_profile(code) for code in (clan_codes or CLAN_ORDER)] if kind=='clans'
            else role_semantics() if kind=='roles' else workflow_meanings())
    return {'rows':rows,'mode':'domain_facts','reference_kind':kind,
        'permission_authority':'elbow_helper/features/agent/access.py',
        'semantic_facts_grant_permissions':False,
        'coverage':'Descriptive sourced facts. Check actual held role IDs and current access for every read or action. '
                   'Restricted narrative policies still require scoped approved knowledge reads.'}
