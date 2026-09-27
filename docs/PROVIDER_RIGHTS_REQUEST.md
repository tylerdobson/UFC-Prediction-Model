# Draft source-rights inquiry — not sent

Recipient: `api@ufcalendar.com` (published in the [Fight API terms](https://www.ufcalendar.com/developers/terms))

Subject: Confirm permitted UFC data use for a private forecasting project

Hello UFCalendar team,

I am evaluating your Fight API for a private, single-operator UFC forecasting application. It would save your event, fight, fighter, result, per-fight-stat, and eligible consensus-odds responses with source timestamps and hashes; train and evaluate statistical fight-winner models; display derived probabilities and the underlying card/result evidence in a private dashboard; and support **manual** pre-fight betting decisions. It would not redistribute a raw feed, set or settle wagers, place wagers, or operate a regulated sportsbook. A separate provider would supply named-bookmaker prices for any decision, rather than using your consensus line as an executable price.

Before I open a paid account or use the data for this purpose, please confirm in writing:

1. Do your [terms last updated September 26, 2026](https://www.ufcalendar.com/developers/terms) and paid Hobby license permit this exact private betting decision-support use, including model training, retained raw responses, internal dashboard display, and indefinite audit storage after a subscription ends? Are there any separate permissions or restrictions for data sourced from UFCStats, Wikipedia, sportsbooks, or other upstream sites?
2. Does the event/card-change API provide the timestamp when a matchup or substitution was first observed, including changes on historical cards? Are historical snapshots or revision logs available, or only today's corrected view?
3. Which identifiers remain stable when a fighter is renamed or merged, and how are prior IDs mapped to a surviving ID? Can a bout ID change after a substitution?
4. Do result and per-fight-stat responses include publication or correction timestamps? How are draws, no contests, cancellations, and overturned results represented?
5. May I read and retain your opening and closing consensus lines solely as a research baseline? Does each price point carry an independently observed UTC timestamp, capture timestamp, source-book count, and correction history? Your [current developer page](https://www.ufcalendar.com/developers) advertises closing lines for 9,400+ completed fights and movement for 2,700+; for which historical years can a 24-hour-before-card consensus be reconstructed without using later prices? If this requires Pro movement history, please confirm its access and retention terms.
6. May the time-limited trial be used solely to test data quality for this proposed application before choosing a plan? If Hobby does not cover any part of the described use, which plan or written agreement would cover it?

I would also appreciate a redacted example of an event with a replacement opponent, its change log, and the corresponding result/stat correction metadata, plus an example of a historical bout with multiple dated consensus price points. Does the change log omit no-op or reverted changes, and are complete historical roster observations available separately? Please identify any attribution requirements for a private dashboard.

Thank you.

## Decision rule

Keep this candidate in evaluation status until the provider answers the rights and timestamp questions. Do not treat a trial key or a marketing feature list as evidence that the project's intended use is licensed or point-in-time complete. Record the response and applicable terms version in the private source register before importing operational data.
