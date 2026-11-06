# Contributing

Pages that tidyscan turns or straightens wrongly are the most useful
contribution. Open an issue with what happened and, if you can, a page that
shows it and that you may share: a test page, not a private document.

For code changes:

- Keep the PDF writer on the standard library; anything that needs a package
  is an optional extra.
- Never re-encode a page image; turn and straighten in the PDF.
- Add a test for every change in behaviour, and for orientation, skew or OCR
  rerun the evaluation scripts in `tests/` and put their numbers in the pull
  request.
- Run the tests (see the README) before opening a pull request.

## AI-assisted contributions

This project itself was written with an AI assistant (Claude, in Claude Code),
with every change reviewed by its maintainer and measured on real pages; the
commits say so. AI-assisted contributions are welcome on the same terms:

- You understand every line you submit and can explain it in the review.
- You have tested it, and measured it where it changes how pages come out.
- You say so: an `Assisted-by:` line in the commit or the pull request, naming
  the tool and model.
- You have the right to submit it under this project's licence.

Not wanted: large unrequested changes that nobody can explain without the tool
that wrote them, and issues or bug reports that no person has checked.

Scans are often private documents. Do not paste real scans or text from them
into an AI tool or into an issue; reproduce the problem with a test page.

Instructions for coding agents are in [AGENTS.md](AGENTS.md).
