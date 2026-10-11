# The evaluation set

Sixty short articles, written for the purpose, each with the band the example student in
`profile.example/` (Ines, the marine biologist) should see it in: ten excellent, eighteen good,
eight middling, ten poor, fourteen junk. Nothing here is a real publication; the names, places
and figures are invented. Every entry carries a `why`: one line on what makes the band, so a
disagreement with the judge can be argued about rather than guessed at.

`bodh eval` judges the set with the configured judge (or `--model`) for the example student,
whatever `profile/about.md` says, and prints the agreement: the share of articles landing in the
expected band, the share within one band of it, the per-band picture, and the ten largest
disagreements with the judge's lesson. It costs a few cents and stops at `--cap` (50 cents by
default). `--write evals/results.json` keeps the full result; the README quotes the agreement
figure with its date and the model.

What the figure means: the bands are one reader's reading of the rubric for one invented
student. Exact agreement in the sixties and agreement within one band in the nineties is what
a careful judge gets; a fall of ten points after a prompt change is worth a look, a rise is not
proof of anything. The set is small on purpose, so that it stays cheap enough to run every week
and after every prompt change (`.github/workflows/evals.yml`, with the owner's key as a secret,
skipped on forks).

To add an article: copy an entry in `set.yaml`, give it the next id, keep the text under 200
words, pick the band for Ines, write the `why`. `tests/test_evals.py` checks the shape offline.
