"""Typed identity and measure columns for checked family result references."""
from ..engine.result_fields import names

COMMON = {**names('schema_version,report_id,total_rows,returned_rows,next_offset,next_cursor,complete_snapshot,count,coverage,missing_sources,resolved_inputs,artifact_status'),
          'artifact': names('report_id,filename,total_rows')}
RESULT_FIELDS = {
    'read_ranked_activity': {**COMMON, 'rows': [{**names('player_tag,member_id,player_name,status,selection_rank,ranked_league,attacks_used,attacks_available,live_status,counts_complete,conflicting_battles'), 'observed_attacks':names('battles,average_stars,average_destruction_percent,triple_rate_percent'), 'observed_defenses':names('battles,average_stars,average_destruction_percent,triple_rate_percent')}]},
    'read_war_attendance': {**COMMON, 'rows': [names('member_id,member_name,player_tag,player_name,account_tags,war_ids,attacks_used,attacks_expected,missed,miss_rate,unconfirmed_opportunities,unit,missing_link')]},
    'read_cwl_results': {**COMMON, 'rows': [names('player_tag,player_name,clan_code,season,league,rank,rank_total,ass_score,score,attacks,attacks_expected,multi_season_score,multi_season_rank')]},
    'read_current_war': {**COMMON, 'rows': [{**names('clan_code,status,state,start_at,end_at,team_size,opponent'), 'members': [names('player_tag,player_name,attacks_used,unused_attacks,missed')]}]},
}

RESULT_FIELDS.update({
    'read_member_accounts': {**COMMON, 'rows': [{**names('member_id,member_name,joined_at,player_tag,player_name,townHallLevel,role,ranked_league,legacy_league,clan,clan_status,trophies,profile_status'), 'roles': [names('role_id,name')]}], **names('member_count,account_count,status')},
    'read_clan_rosters': {**COMMON, 'rows': [names('clan_code,basis,season,player_tag,player_name,townHallLevel,role,donations,profile_status')]},
    'read_member_cohort': {**COMMON, 'rows': [{**names('member_id,member_name,joined_at'), 'accounts':[names('player_tag,player_name,townHallLevel,ranked_league,profile_status')], 'roles': [names('role_id,name')]}]},
})

# Keep composition paths aligned with the complete retained family contracts.
RESULT_FIELDS['read_war_attendance'].update(names('war_ids,ranked_population,opportunity_unit'))
RESULT_FIELDS['read_war_attendance']['rows'][0].update(names('totals_complete'))
RESULT_FIELDS['read_war_attendance']['rows'][0].update(names('participation_status'))
RESULT_FIELDS['read_war_attendance']['clan_participation'] = [{
    **names('clan_code,wars_observed'),
    **{status: {**names('member_count,unlinked_account_count'),
                'identities': [names('member_id,member_name,player_tag,player_name,account_tags,unit')]}
       for status in ('warring', 'joined_without_attacking', 'unknown')}}]
RESULT_FIELDS['read_cwl_results']['current_clan_leagues'] = [
    {**names('clan_code,status,observed_at,season_league,basis'), 'current_league': names('id,name')}]
RESULT_FIELDS['read_member_accounts']['rows'][0].update(names('nickname,war_stars,observed_at,donations'))
RESULT_FIELDS['read_member_accounts'].update(
    history_reads={kind: names('read_ran,status,after,before,error,cause,coverage,history_coverage')
                   for kind in ('regular_war', 'cwl', 'ranked')},
    name_resolutions=[{**names('query,status,account_status,member_status'),
        'matching_members': [{**names('member_id,member_name,username,nickname,match_sources,account_links_status'),
                              'accounts': [names('player_tag,player_name')]}],
        'candidates': [names('player_tag,player_name,names,sources,member_id,member_name,membership_status')]}])
for family in ('read_clan_rosters', 'read_member_accounts', 'read_war_attendance'):
    RESULT_FIELDS[family]['rows'][0].update(
        leadership={'status': None, 'roles': [names('role_id,name')]})
for family in ('read_member_accounts', 'read_member_cohort'):
    RESULT_FIELDS[family]['rows'][0].update(
        has_elbow_helper_links=None,
        family_membership_roles={name: names('role_id,held') for name in ('member', 'trial')})
    RESULT_FIELDS[family]['rows'][0].update(
        **names('home_clan_status,home_clan_basis'),
        home_clans=[names('clan_code,name,tag,role_id,role_name')])
    RESULT_FIELDS[family]['rows'][0]['roles'][0]['clan_purposes'] = [names('clan_code,purpose')]
    RESULT_FIELDS[family]['rows'][0]['participation_clans'] = RESULT_FIELDS[family]['rows'][0]['home_clans']
RESULT_FIELDS['read_member_accounts']['rows'][0].update(
    membership_status=None, home_role_checks=[names('clan_code,role_id,has_role')])
RESULT_FIELDS['read_member_accounts'].update(names('role_comparison_complete'))
RESULT_FIELDS['read_member_accounts']['home_role_comparisons'] = [{
    **names('member_id,member_name,player_tag,player_name,membership_status'),
    'home_role_checks': [names('clan_code,role_id,has_role')]}]
RESULT_FIELDS['read_cwl_results']['clan_results'] = [{
    **names('clan_code,league,completed_wars,scoring_status,ranked_population'),
    'top_rows': RESULT_FIELDS['read_cwl_results']['rows']}]
RESULT_FIELDS['read_member_accounts']['unknown_accounts'] = RESULT_FIELDS['read_member_accounts']['rows']
RESULT_FIELDS['read_clan_rosters']['clans'] = [names('clan_code,name,tag,basis,season,status,cohort_accounts,included_accounts,excluded_accounts,unknown_filter_accounts,in_game_members,in_game_capacity,capacity_basis,open_in_game_slots,town_halls')]
RESULT_FIELDS['read_clan_rosters']['clans'][0]['entry_settings'] = names(
    'type,join_method,type_meaning,requiredTownhallLevel,requiredTrophies,requiredBuilderBaseTrophies')
RESULT_FIELDS['read_member_cohort']['rows'][0].update(names('accepted_at,selection_status'))
RESULT_FIELDS['read_member_cohort'].update(names('selected_role_ids'))
RESULT_FIELDS['read_member_cohort']['rows'][0].update(
    **names('donations,membership_status'),
    war_activity=names('status,wars_observed,attacks_used,attacks_expected,missed,attack_completion_percent,unconfirmed_opportunities,totals_complete'),
    chat_activity=names('status,latest_observed_at,source'))
RESULT_FIELDS['read_member_cohort']['rows'][0]['accounts'][0].update(names('clan_code,donations,clan_status'))
RESULT_FIELDS['read_member_cohort']['rows'][0]['accounts'][0]['war_activity'] = RESULT_FIELDS['read_member_cohort']['rows'][0]['war_activity']
RESULT_FIELDS['read_member_cohort']['rows'][0]['accounts'][0]['clan'] = names('tag,name')
RESULT_FIELDS['read_member_cohort']['rows'][0].update(names(
    'account_tags,account_names,clan_codes,war_status,wars_observed,attacks_used,attacks_expected,missed,attack_completion_percent,war_totals_complete,chat_status,latest_chat_at'))
RESULT_FIELDS['read_member_cohort']['activity_coverage'] = {
    'chat_coverage': names('members_checked,members_not_checked,source_reads_complete,channel_count')}
RESULT_FIELDS['read_member_cohort']['clans'] = [names(
    'clan_code,name,tag,basis,status,cohort_accounts,included_accounts,excluded_accounts,unknown_filter_accounts,in_game_members,in_game_capacity,open_in_game_slots')]
RESULT_FIELDS['read_discord_access'] = {**COMMON, 'rows': [names(
    'channel_id,channel_name,actor_kind,actor_id,actor_name,guild_permissions,channel_permissions,view_channel,read_message_history,role_only')]}

RESULT_FIELDS.update({
 'read_recruitment_pipeline': {**COMMON, 'rows': [names('member_id,applicant_member_id,ticket_channel_id,workflow_status,start_at,expected_end_at,trial_ended_at,acceptance_decision,finalized,message_id,content,source')]},
 'read_hibernation': {**COMMON, 'rows': [{**names('member_id,member_name,joined_at,started_at,started_ts,start_time_status'), 'accounts': [names('player_tag,profile_status,observed_ranked_attacks_since_start,latest_observed_ranked_attack_at')]}]},
 'read_promotion_cases': {**COMMON, 'rows': [names('ticket_channel_id,applicant_member_id,case_type,workflow_status,intake_status,routing_status,response_status')]},
})

WORKFLOW_EVENT = names('member_id,applicant_member_id,ticket_channel_id,evidence_kind,workflow_status,start_at,expected_end_at,trial_ended_at,message_id,content,source')
RESULT_FIELDS['read_recruitment_pipeline']['rows'][0].update(
    **names('accepted_clan_codes,participation_clan_codes,placement_basis,start_at,embeds,attachment_names'),
    timeline=[WORKFLOW_EVENT], accounts=RESULT_FIELDS['read_member_accounts']['rows'],
    period_status=None, period_evidence=[WORKFLOW_EVENT], undated_evidence=[WORKFLOW_EVENT])
RESULT_FIELDS['read_recruitment_pipeline']['source_next_cursor'] = None
RATIO = names('numerator,denominator,value,display')
PERFORMANCE = {**names('wars,observed_attacks,measurement_status,accounts'),
               'average_stars': RATIO, 'average_destruction_percent': RATIO, 'triple_rate_percent': RATIO}
RESULT_FIELDS['read_war_attendance']['rows'][0]['attack_performance'] = {
    **PERFORMANCE, **names('stars,stars_measured,triples')}
RESULT_FIELDS['read_member_accounts']['rows'][0]['activity'] = {
    kind: {**names('status,error,cause'), **RESULT_FIELDS[family]['rows'][0]}
    for kind, family in (('regular_war', 'read_war_attendance'), ('cwl', 'read_war_attendance'),
                         ('ranked', 'read_ranked_activity'))}
RESULT_FIELDS['read_cwl_results']['rows'][0].update(names('member_id,roster_id,cycle_id,townHallLevel,signed_up_ts'))
RESULT_FIELDS['read_cwl_results']['rosters'] = [{**names('roster_id,name,clan_code,cycle_id,cycle_key,status,accounts'),
    'source_posts': [names('channel_id,message_id')]}]
RESULT_FIELDS['read_recruitment_pipeline']['rows'][0].update(
    war_attendance=RESULT_FIELDS['read_war_attendance']['rows'][0],
    attack_performance={'REG': PERFORMANCE, 'CWL': PERFORMANCE})
RESULT_FIELDS['read_promotion_cases']['rows'][0].update(review=names('ticket_channel_id,review_channel_id,text'),
                                                      review_status=None, war_attendance=RESULT_FIELDS['read_war_attendance']['rows'][0])
RESULT_FIELDS['read_hibernation']['rows'][0]['accounts'][0].update(names(
    'observed_regular_war_attacks_since_start,observed_cwl_attacks_since_start,latest_observed_regular_war_at,latest_observed_cwl_at'))

RESULT_FIELDS.update({
 'read_community_reference': {**COMMON, 'rows':[names('section_id,title,file_name,visibility,body,clan_code,name,tag,branch,purpose,configured_cwl_only,operational_cwl_only,has_placeholders,membership_basis,town_hall_min,town_hall_max,subject,holds,event,establishes,does_not_establish')]},
 'read_conversation_evidence': {**COMMON, 'rows':[names('message_id,channel_id,author_id,author,timestamp,content,source,reply_to_message_id,content_complete')], **names('source_next_cursor,delivered_answer')},
})

RESULT_FIELDS['read_conversation_evidence'].update(names('remaining_message_id,projection,selection_complete'))
RESULT_FIELDS['read_community_reference']['rows'][0].update(names(
    'workflow,sources,formula,profiles,profile_selection,support,recruitment,requester_lookup,new_applicant_route,supported_clan_codes'))
RESULT_FIELDS['read_community_reference']['rows'][0]['roles'] = [names('role_id,name,purpose,status')]
RESULT_FIELDS['read_community_reference']['access_limits'] = [{
    'reader_roles': [names('role_id,name')], 'available_privately': None}]
RESULT_FIELDS['read_conversation_evidence']['rows'][0].update(
    **names('attachment_id,filename,content_type,size,attachment_contents_read'),
    embeds=[{'title':None,'description':None,'author':{'name':None},'footer':{'text':None},
             'fields':[names('name,value')]}],
    attachment_names=[''],
    attachments=[names('attachment_id,filename,content_type,size,url')],
    conversation={**names('requester_id,request_message_id,request,delivered_answer,delivery_complete,report_ids'),
                  'resolved_inputs': [names('family,report_id,selection,observed_at')]})
