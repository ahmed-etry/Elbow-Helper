"""Approved implementation facts for the planner and writer, never reply templates.

Keep the formula aligned with build_ass_season_metrics; profile values and
supported recruitment clans are read from their owning modules.
"""
from dataclasses import asdict

from elbow_helper.configuration.clans import CLAN_INFO_BOARDS
from elbow_helper.configuration.channels import TRIAL_LIST, TICKETS_LOG
from elbow_helper.configuration.roles import LEAD_PLUS
from elbow_helper.features.cwl.roster.models import ASS_PROFILE_HIGH, ASS_PROFILE_LOWER
from elbow_helper.features.recruitment.config import TRIAL_DAYS_DEFAULT
from elbow_helper.features.clan_transfers.config import CLAN_TRANSFER_QUEUES, CWL_SEASON_TRANSFER_CLANS


def bot_workflows(context=None):
    from ..automatic_workflows import automatic_workflows
    roster_permission = {'role_group': 'LEAD_PLUS', 'any_role_ids': sorted(LEAD_PLUS)}
    if context is not None:
        roster_permission.update(
            requester_allowed=any(role.id in LEAD_PLUS for role in context.member.roles),
            current_roles=[{'role_id': role.id, 'name': getattr(role, 'name', None)}
                           for role in context.guild.roles if role.id in LEAD_PLUS])
    return {'mode': 'implementation_reference', 'rows': [
        {'workflow': 'ass_scoring',
         'sources': ['elbow_helper/features/cwl/roster/analysis.py:build_ass_season_metrics',
                     'elbow_helper/features/cwl/roster/analysis.py:profiles_for_roster_history',
                     'elbow_helper/features/cwl/roster/models.py:AssProfile',
                     'elbow_helper/features/cwl/queries.py:score_ass_wars'],
         'formula': {
             'projected_stars': '7 * total_stars / attacks_used',
             'projected_missed_stars': '7 * (3 * attacks_used - total_stars) / attacks_used',
             'difficulty_per_attack': '((team_size + 1) / 2 - defender_map_position) * (15 / team_size) * difficulty_weight',
             'difficulty_adjustment': 'mean(difficulty_per_attack for attacks with both map positions)',
             'ass_score': '(projected_stars + missed_adjustment + difficulty_adjustment) * (average_destruction_percent / 100)',
             'high_missed_adjustment': '0 if projected_missed_stars <= 1 else -2 * (projected_missed_stars - 1)',
             'lower_missed_adjustment': '0 if projected_missed_stars <= 2 else -projected_missed_stars'},
         'profiles': [asdict(ASS_PROFILE_HIGH), asdict(ASS_PROFILE_LOWER)],
         'profile_selection': 'Most recent completed CWL league in the selected history: Champion or Master I uses high; others use lower.',
         'zero_attacks': 'unscored', 'missing_positions_difficulty_adjustment': 0,
         'ranking_scope': ['clan', 'season'], 'partial_sample_projects_to_attacks': 7,
         'same_clan_ass': 'mean of scored seasonal ASS values in the same clan',
         'historical_workbooks': 'Historical report settings may differ from the current implemented scorer.',
         'implemented_cherry_pick_adjustment': False},
        {'workflow': 'ticket_opening',
         'sources': ['elbow_helper/features/support_tickets/commands.py:prepare_support_ticket',
                     'elbow_helper/features/support_tickets/commands.py:open_support_ticket',
                     'elbow_helper/features/support_tickets/ai.py:SupportWelcomeService.create',
                     'elbow_helper/features/recruitment/tickets.py:on_guild_channel_create',
                     'elbow_helper/features/recruitment/ai.py:_process_applicant_ticket',
                     'elbow_helper/features/recruitment/commands.py:prepare_checkup'],
         'support': {'command': '/open', 'options': ['user', 'topic'],
                     'operator_group': 'Lead', 'automatic_welcome': True,
                     'welcome_trigger': 'Opening the support ticket posts the member mention, topic-based welcome, Support Ticket embed and close control.',
                     'welcome_generation': 'One neutral sentence about the supplied topic; existing fallback when generation fails.',
                     'separate_welcome_command_required': False},
         'recruitment': {'opening': 'Application ticket opener belongs to the application flow.',
                         'automatic_processing': 'New recruitment tickets start applicant review after reading application answers; Applicant Review is posted in the recruitment room.',
                         'conversation_command': '/checkup',
                         'acceptance_welcome_command': '/accept'},
         'requester_lookup': {'family': 'read_recruitment_pipeline', 'stage': 'requester_tickets',
                              'ownership': 'Registered applicant ID or unique member mention on a bot-authored opener; never channel-name similarity.',
                              'visibility': 'Requester and bot must be able to read the ticket; destination scope still applies.'}},
        {'workflow': 'recruit_placement',
         'sources': ['elbow_helper/features/recruitment/commands.py:prepare_accept',
                     'elbow_helper/features/recruitment/commands.py:accept_applicant',
                     'elbow_helper/configuration/clans.py:CLAN_INFO_BOARDS',
                     'elbow_helper/features/help/catalog.py:/accept'],
         'new_applicant_route': 'recruitment ticket', 'acceptance_command': '/accept',
         'acceptance_options': ['applicant', 'clans', 'nickname', 'player_tags', 'days', 'channel', 'additional_notes'],
         'supported_clan_codes': list(CLAN_INFO_BOARDS), 'default_trial_days': TRIAL_DAYS_DEFAULT,
         'confirmation_required': True, 'clan_promotion_ticket_required_for_new_applicant': False,
         'placement_authority': 'Supported clan options establish implementation support, not permission to approve a particular applicant. Read recruitment placement policy and command access.',
         'existing_member_promotion': 'Separate examination/promotion workflow; not the intake route for a new recruit.'},
        {'workflow': 'roster_administration',
         'sources': ['elbow_helper/features/rosters/ui/views.py:RosterSettingsView.run_action',
                     'elbow_helper/features/rosters/ui/views.py:RosterRemovalView.confirm',
                     'elbow_helper/features/rosters/cog.py:Rosters.is_lead',
                     'elbow_helper/features/rosters/cog.py:Rosters.remove_roster_players'],
         'entry_point': '/roster post',
         'remove_signups': ['Open the posted roster gear menu', 'Remove accounts',
                            'Select the accounts', 'Remove'],
         'operator_role_ids': sorted(LEAD_PLUS),
         'self_opt_out_required': False,
         'scope': 'Selected roster signups; not Discord membership or in-game clan membership.'},
        {'workflow': 'roster_signup_management',
         'sources': ['elbow_helper/features/rosters/cog.py:Rosters.is_lead',
                     'elbow_helper/features/rosters/cog.py:Rosters.remove_roster_players',
                     'elbow_helper/features/rosters/ui/views.py:RosterSettingsView.run_action',
                     'elbow_helper/features/rosters/ui/views.py:RosterRemovalView',
                     'elbow_helper/configuration/roles.py:LEAD_PLUS'],
         'scope': 'Posted war and CWL signup rosters; selected Clash accounts belonging to any member.',
         'removal': {'entry_point': 'Gear menu on the posted roster',
                     'control': 'Remove accounts', 'selection': 'Select accounts; optionally filter by Discord member.',
                     'confirm_control': 'Remove', 'permission': roster_permission},
         'operator_roles': ['Co-Leader', 'Cross-Leader', 'Co Applicant'],
         'self_service': {'control': 'Opt-out', 'scope': 'The member selects their own signed-up accounts.'}},
        {'workflow': 'account_linking',
         'sources': ['faq.md:Adding a village and leaving an unused account linked',
                     'faq.md:Joining tickets and adding alts to the family'],
         'existing_member_route': '/profile',
         'existing_member_steps': ['Open /profile', 'Link the additional Clash account'],
         'new_applicant_route': 'get-started: Join the family',
         'existing_applicant_route': 'Link additional alts in the existing joining ticket',
         'new_application_required_for_existing_member': False,
         'unused_account_may_remain_linked': True,
         'placement': 'Account linking does not approve clan placement; ask leadership about placement.'},
        {'workflow': 'discord_transfer_requests',
         'sources': ['elbow_helper/features/clan_transfers/commands.py:transfer_request_preview',
                     'elbow_helper/features/clan_transfers/commands.py:request_transfer',
                     'elbow_helper/features/clan_transfers/config.py:CWL_SEASON_TRANSFER_CLANS'],
         'command': '/transfer request', 'scope': 'Discord destination queue, not an in-game join request',
         'destinations': [{'clan_code': code, 'cwl_season_only': code in CWL_SEASON_TRANSFER_CLANS}
                          for code in CLAN_TRANSFER_QUEUES],
         'seasonal_window': {'start_days_before_month': 2, 'end_day_of_month_inclusive': 11},
         'other_destinations_have_seasonal_limit': False},
        {'workflow': 'recruitment_history',
         'sources': ['elbow_helper/features/recruitment/trials.py:_build_trial_tracking_embed',
                     'elbow_helper/features/recruitment/trials.py:check_expired_trials',
                     'elbow_helper/features/recruitment/trials.py:end_trial_ticket',
                     'elbow_helper/features/recruitment/helpers.py:_get_clan_role_mentions',
                     'elbow_helper/configuration/channels.py'],
         'tracking_channel_id': TRIAL_LIST, 'ticket_log_channel_id': TICKETS_LOG,
         'tracking_embed': {'title': 'Active Trial',
             'fields': ['Applicant', 'Ticket', 'Clans', 'Ends', 'Started'],
             'placement_basis': 'Configured clan role mentions recorded at trial start; utility roles are extra participation.'},
         'active_trial_json_is_monthly_history': False,
         'tracking_retention': 'Active trial records and tracking cards are deleted when tracking ends, including expiry and End Now.',
         'acceptance_history_stage': 'acceptance_history', 'transcript_stage': 'ticket_log',
         'decision_basis': 'Read the relevant transcript for accepted/declined outcomes; closure alone is not a decision.',
         'join_cohort_contents': 'Server join date, current Member/Trial roles, home clan roles, Elbow Helper link presence and linked accounts with current clans.'},
        *automatic_workflows(),
    ]}
