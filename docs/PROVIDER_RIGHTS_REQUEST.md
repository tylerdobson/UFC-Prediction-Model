# Draft source-rights inquiry — not sent

Recipient: `api@ufcalendar.com` (published in the [Fight API terms](https://www.ufcalendar.com/developers/terms))

Subject: Confirm permitted UFC data use for a private forecasting project

Hello UFCalendar team,

I am evaluating your Fight API for a private, single-operator UFC forecasting application. It would save your event, fight, fighter, result, and per-fight-stat responses with source timestamps and hashes; train and evaluate statistical fight-winner models; display derived probabilities and the underlying card/result evidence in a private dashboard; and support **manual** pre-fight betting decisions. It would not redistribute a raw feed, compile sportsbook odds, place wagers, or operate a regulated sportsbook.

Before I open a paid account or use the data for this purpose, please confirm in writing:

1. Does your paid Hobby license permit this exact private betting decision-support use, including model training, retained raw responses, internal dashboard display, and indefinite audit storage? Are there any separate permissions or restrictions for data sourced from UFCStats or other upstream sites?
2. Does the event/card-change API provide the timestamp when a matchup or substitution was first observed, including changes on historical cards? Are historical snapshots or revision logs available, or only today's corrected view?
3. Which identifiers remain stable when a fighter is renamed or merged, and how are prior IDs mapped to a surviving ID? Can a bout ID change after a substitution?
4. Do result and per-fight-stat responses include publication or correction timestamps? How are draws, no contests, cancellations, and overturned results represented?
5. May a time-limited trial be used solely to test data quality for this proposed application before choosing a plan? If the Hobby plan is insufficient, which plan or agreement covers it?

I would also appreciate a redacted example of an event with a replacement opponent, its change log, and the corresponding result/stat correction metadata. Please identify any attribution requirements for a private dashboard.

Thank you.

## Decision rule

Keep this candidate in evaluation status until the provider answers the rights and timestamp questions. Do not treat a trial key or a marketing feature list as evidence that the project's intended use is licensed or point-in-time complete. Record the response and applicable terms version in the private source register before importing operational data.
