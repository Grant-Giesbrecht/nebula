"""
ex3 -- everything `sess.artifact()` can record, and why you would bother.

ex2 writes a file. This one writes files that can answer questions later:
what produced this, what did it come from, what were the settings, which
run does it belong with, what do I call it in a paper.

Nothing here is required. A bare `with sess.artifact("x.tome") as fn:`
already captures the script, the git commit, the timestamp and a
checksum -- everything below is what you add when you know something the
machine cannot work out on its own.

Run it against a scratch archive:

    nebula init /tmp/ex3-archive --name ex3
    python examples/ex3_provenance.py /tmp/ex3-archive
"""

import sys
from pathlib import Path

import nebula
from nebula import assets, collection, uris

ARCHIVE = sys.argv[1] if len(sys.argv) > 1 else "postdoc"

# A registered nickname needs checking (it may point somewhere that has
# since moved); a literal path speaks for itself. validate_archive only
# knows about the first kind.
if not Path(ARCHIVE).is_dir():
	nebula.validate_archive(ARCHIVE)
else:
	ARCHIVE = Path(ARCHIVE)


def fake_data(n, scale=1.0):
	"""Stand-in for a measurement. Any list will do for this example."""
	return [scale * (i % 7) for i in range(n)]


def write_csv(path, rows):
	Path(path).write_text("\n".join(f"{i},{v}" for i, v in enumerate(rows)))


# `new_session=True` keeps this example unattended -- no picker, no
# prompts. A real script would just call nebula.session(ARCHIVE) and let
# the picker (and then the tags/description prompt) run. `artifact_tags`
# is applied to every file below, on top of each file's own tags.
with nebula.session(
	ARCHIVE,
	new_session=True,
	tags=["example"],
	description="Everything artifact() can record",
	artifact_tags=["ex3"],
) as sess:

	print(f"writing into session {sess.id} at {sess.path}")

	# -----------------------------------------------------------------
	# 1. inputs -- the settings that produced the file
	# -----------------------------------------------------------------
	# Anything you'd otherwise write in a lab notebook or bury in a
	# filename. These are stored in the sidecar and shown by
	# `nebula show`, so "was this the 10 dB run?" stops being a guess.
	#
	# Keep them JSON-able (numbers, strings, lists, dicts). They are a
	# record, not a pickle.
	settings = {"gain_db": 10, "averages": 64, "span_ghz": [4.0, 8.0]}

	with sess.artifact("scope_trace.csv", inputs=settings) as fn:
		write_csv(fn, fake_data(64))

	# -----------------------------------------------------------------
	# 2. derived_from -- what this file came out of
	# -----------------------------------------------------------------
	# The edge that makes provenance a graph instead of a pile. A bare
	# filename means "the file of that name in THIS session", which is
	# the overwhelmingly common case:
	with sess.artifact(
		"fit.tome",
		derived_from=["scope_trace.csv"],
		inputs={"model": "lorentzian", "seed": 7},
		tags=["fit"],
		comment="Converged on the second try; first seed diverged.",
	) as fn:
		Path(fn).write_text("{}")

	# derived_from is a list -- several parents are normal:
	with sess.artifact(
		"summary.csv",
		derived_from=["scope_trace.csv", "fit.tome"],
	) as fn:
		write_csv(fn, fake_data(4))

	# It also takes refs beyond this session. Each of these is a URI with
	# some leading parts left off, meaning "here":
	#
	#   raw.csv                            this session
	#   S-26-0152/raw.csv                  another session, this archive
	#   nebula://postdoc~0fe/S-26-0152/raw.csv     another archive of mine
	#   nebula://grant@github.com/postdoc~0fe/S-26-0152/raw.csv  someone else's
	#
	# Nothing checks that the target exists at write time (the archive may
	# not be mounted). `nebula check` is what reports dangling refs.

	# -----------------------------------------------------------------
	# 3. tags and comments -- the mutable half
	# -----------------------------------------------------------------
	# Tags and comments are NOT sidecar fields. A sidecar records what
	# happened and is never rewritten; a tag is something you change your
	# mind about, so they live in the session's annotations.yaml, which
	# `nebula annotate` and the Navigator edit freely.
	#
	# Setting them here is just a convenience: write time is when you
	# actually know what the file is.
	with sess.artifact("figure-3.csv", tags=["paper-2026", "final"]) as fn:
		write_csv(fn, fake_data(16))

	# Changed your mind afterwards? Same field, additive for tags:
	sess.annotate("figure-3.csv", tags=["thesis-ch3"])
	sess.annotate(tags=["reviewed"], comment="Session notes go here.")

	# -----------------------------------------------------------------
	# 4. extra keyword arguments -- your own sidecar fields
	# -----------------------------------------------------------------
	# Anything else you pass lands in the sidecar verbatim, alongside the
	# fields nebula defines. Use it for facts about the measurement that
	# aren't settings: instrument serials, ambient conditions, an
	# operator's initials.
	with sess.artifact(
		"warmup.csv",
		inputs={"minutes": 20},
		instrument="Keysight N5242B",
		fridge_stage_mk=12.4,
		operator="GG",
	) as fn:
		write_csv(fn, fake_data(32))

	# -----------------------------------------------------------------
	# 5. announce -- the per-save report
	# -----------------------------------------------------------------
	# On by default: each save prints its URI, path, size and tags. Turn
	# it off for the thousandth file of a sweep and back on for the one
	# result the run is about.
	for i in range(3):
		with sess.artifact(f"sweep-{i:03d}.csv", announce=False) as fn:
			write_csv(fn, fake_data(8, scale=i))

	with sess.artifact("sweep-result.tome",
	                   derived_from=[f"sweep-{i:03d}.csv" for i in range(3)],
	                   announce=True) as fn:
		Path(fn).write_text("{}")

	# NEBULA_ANNOUNCE=0 in the environment silences the lot, and
	# nebula.session(..., announce=False) silences one session.

	# -----------------------------------------------------------------
	# 6. assets -- deriving from something that is allowed to change
	# -----------------------------------------------------------------
	# An artifact is sealed; an asset is a file you keep editing (a
	# calibration table, a mask, a schematic). Saying "derived from the
	# calibration" would otherwise name a file whose contents are free to
	# change afterwards -- so nebula pins the bytes it actually saw at
	# write time, and records the checksum on the edge.
	cal = Path(sess.path) / "_calibration.csv"
	write_csv(cal, fake_data(8))
	asset = assets.import_asset(sess.archive_root, cal, name="calibration.csv",
	                            origin="ex3 example", move=True)
	print(f"imported asset {asset.id}")

	with sess.artifact("corrected.csv",
	                   derived_from=[f"assets/{asset.id}"]) as fn:
		write_csv(fn, fake_data(64, scale=0.5))

	# -----------------------------------------------------------------
	# 7. related_runs -- a sibling, not a parent
	# -----------------------------------------------------------------
	# For "this run goes with that one" where neither produced the other:
	# a control measurement, the same sweep at a different temperature.
	# It is a session-level statement, so it is not a derived_from edge.
	#
	#   sess.add_related_run("S-26-0152")
	#   sess.add_related_run("nebula://postdoc~0fe/S-26-0152")

	# -----------------------------------------------------------------
	# 8. artifact_path + write_meta_for -- the escape hatch
	# -----------------------------------------------------------------
	# When a library insists on opening the file itself, or writes several
	# files at once, you cannot wrap it in `with`. Get the path, let it
	# write, then record the metadata yourself. Everything artifact()
	# takes, write_meta_for() takes.
	#
	# The `with` form is preferred because it cannot drift: it refuses to
	# finish if no file appeared, so a silently-failed write is loud
	# rather than an untracked hole.
	path = sess.artifact_path("external.csv")
	write_csv(path, fake_data(12))
	sess.write_meta_for("external.csv",
	                    derived_from=["scope_trace.csv"],
	                    inputs={"written_by": "a stubborn library"},
	                    tags=["hand-recorded"])

	# Anything written into the session folder with NEITHER of these gets
	# reported at close() -- see nebula.session(on_missing_meta=...).

	# -----------------------------------------------------------------
	# 9. the URI -- what to put in the paper
	# -----------------------------------------------------------------
	# Stable across renaming the archive AND moving it on disk: the
	# archive's segment carries an immutable id (`postdoc~0fe`), and
	# resolution uses the id, not the label.
	info = uris.describe(sess.archive_root, session=sess.id, file="fit.tome")
	print(f"\nfit.tome is {info.uri}")
	for warning in info.warnings:
		print(f"  note: {warning}")

	# `nebula uri <archive> <session>/<file>` prints the same thing, and
	# the Navigator has right-click > Get URI.

	# -----------------------------------------------------------------
	# 10. collections -- a named set that outlives the session
	# -----------------------------------------------------------------
	# Sessions group by when. Collections group by what for -- everything
	# that went into one paper, across as many sessions as it took.
	collection.create(sess.archive_root, "ex3-demo",
	                  title="Files this example wrote")
	collection.add(sess.archive_root, "ex3-demo", f"{sess.id}/fit.tome",
	               note="the one worth keeping")

print("\ndone. Try:")
print(f"  nebula show {ARCHIVE} {sess.id}")
print(f"  nebula upstream {ARCHIVE} {sess.id} summary.csv")
print(f"  nebula annotate {ARCHIVE} {sess.id} figure-3.csv")
print(f"  nebula search {ARCHIVE} \"user_tag:paper-2026\"")
print(f"  nebula collection {ARCHIVE} show ex3-demo")
