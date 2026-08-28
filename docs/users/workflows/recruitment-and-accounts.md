# Recruitment and account links

## Recruitment flow

1. The application answers appear in the recruitment ticket.
2. The bot summarizes those answers for recruiters.
3. `/checkup` starts the recruitment conversation, directs the applicant to the
   rules, and includes any remaining setup steps.
4. Recruiters choose how to proceed:
   - `/accept` handles the applicant's setup, starts their trial, and sends their
     clan links and next steps.
   - `/decline` sends the applicant the decision and labels their ticket as
     declined.
5. The bot tracks each recruit's trial and reminds recruiters when the trial
   period is over. If recruiters are ready to keep the recruit, ending the
   trial asks the recruit whether they want to stay and how it went.
6. After the recruit confirms, `/finalize` completes their setup and gives them
   full member access.

## Account links

An account link connects one Clash account to one Discord member. A member can
have several linked accounts.

- `/account add` links one or more tags. A tag already linked elsewhere moves
  to the selected member.
- `/account list` shows a member's accounts or finds the member linked to a tag.
- `/account remove` unlinks the selected Clash accounts.

The same links are used across recruitment, rosters, CWL, wars, records, and
reports.

## Recruiter tools

`/opinion` gives an AI second opinion based on the application and conversation
in an applicant's ticket.

`/recstats` posts the week's applicant counts by recruitment source and removes
the post after 24 hours. Separate automatic reports cover overdue applicants
and longer-term recruitment activity.
