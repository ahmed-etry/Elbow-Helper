"""Stable sourced descriptions of community records, independent of any request."""

COMMUNITY_DATA_CONTEXT = """<community_data>
Sources below are relative to elbow_helper/features/; K denotes approved community knowledge.
Lifecycle: server join -> application ticket -> /accept -> trial -> full Member after
review and /finalize. A Discord join alone is not an application or family membership.
/accept adds Trial and destination clan roles, creates Elbow Helper account links and a
trial card. Member or Trial identifies current family membership. [recruitment/commands.py:
perform_accept,prepare_finalize; K/recruitment-promotions.md:Apply to join Brown Elbow;
K/recruitment-recruiter.md:End of trial: feedback, response and finalization]
Record lifetimes: active trial JSON and trial cards are deleted when tracking ends,
including expiry/End Now; absence cannot establish no application or no acceptance.
End of tracking does not itself finalize membership. Closed recruitment tickets have
saved transcripts in tickets-log; their contents establish decisions, closure alone does
not. [recruitment/trials.py:end_trial_ticket,check_expired_trials;
K/recruitment-recruiter.md:Recruitment tickets: closing and transcripts]
Elbow Helper links are created by /accept and can also be added through other routes.
No Elbow Helper links means the member has not gone through /accept; links alone do not
prove they have. An unavailable link store is unknown, not no links. ClashPerk links are
separate. [recruitment/commands.py:perform_accept; account_links/cog.py;
K/recruitment-promotions.md:Apply to join Brown Elbow]
Home clan means the configured Discord clan member role; multiple home roles remain
explicit and linked accounts' current clans can clarify them. Current account location
is not historical placement. Utility roles are extra participation: BEW/War Specialist
means extra wars, not a home placement. [agent/families/identity_reads.py:member_fields;
agent/families/domain_facts.py:clan_profile; ../configuration/clans.py]
Read contents (all obey current access and retain source gaps):
- read_member_cohort: current role holders or join-date members; joined_discord includes
  dates, Member/Trial flags, home roles, Elbow Helper link presence and live account clans.
  accepted_recruitment contains surviving active trials, not historical acceptances.
- read_member_accounts: linked accounts, Discord owners/home roles and live Clash profiles.
- read_clan_rosters: current or seasonal account rosters, per-clan counts and live entry settings.
  [agent/families/identity_reads.py]
- read_recruitment_pipeline: active tickets/trials/follow-ups; acceptance_history reads
  surviving trial cards, ticket_log reads saved transcript messages and attachment locators.
  [agent/families/workflow_reads.py; agent/families/recruitment_history.py]
- read_war_attendance: stored completed-war lineups, attacks/misses and per-clan participation.
- read_cwl_results: season performance/ASS, leagues or signed-up/assigned account rosters.
- read_current_war: current lineups and attacks still due. [agent/families/war_reads.py]
- read_ranked_activity: stored ranked attacks with collection gaps. [agent/families/ranked_reads.py]
- read_hibernation: active hibernation and observed activity since its start.
- read_promotion_cases: intake, examiner routing and recorded responses. [agent/families/workflow_reads.py]
- read_community_reference: approved knowledge and sourced clan/role/workflow meanings.
- read_conversation_evidence: authorized message chunks, causal replies and attachment contents.
  [agent/families/reference_reads.py]
- read_discord_access: current role/member permissions and channel access. [agent/families/access_reads.py]
</community_data>"""
