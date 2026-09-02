"""
ex2 -- the shape of an ordinary measurement script.

Open a session, take some data, write it as an artifact. See
ex3_provenance.py for what `sess.artifact()` can record beyond the bytes.
"""

from nebula.cli import AUTO_INTAKE_NICKNAME
import nebula
from stardust.algorithm import randrange
from stardust.tome import dict_to_tome

# Set archive to auto-intake and validate
ARCHIVE = AUTO_INTAKE_NICKNAME
nebula.validate_archive(ARCHIVE)

# There are two kinds of tags, and the difference is worth getting right
# once:
#
#   session tags   describe the RUN  -- "warm-up", "RP23D", "thesis-ch3"
#   artifact tags  describe the FILE -- "shows-drift", "final-figure"
#
# `artifact_tags` below is the second kind: they are attached to every
# file this script writes, whether we start a new session or append to one
# already in progress. That is why it is safe to ask for them up front --
# unlike session tags, they never turn out to be irrelevant.
#
# You can hand-write a list of tags, or use the picker. The picker is
# recommended for tags chosen at run time: it lists the tags the archive
# already uses, TAB-completes them, and flags anything new (so a typo is
# visible before it is saved).
file_tags = ["nebula", "from_intake"]      # tags are a list of strings
file_tags = nebula.input_tag(ARCHIVE)      # ...or run the picker

# Note what is NOT passed here. `nebula.session()` shows the session
# picker first -- append to a session already in progress, or start a
# fresh one -- and only then, if a new session is actually being made,
# asks for its tags and description. Supplying them here would mean
# answering a question that gets thrown away whenever you pick an
# existing session (which already has its own tags and description).
#
# If this script runs unattended, nothing is asked and a new session is
# made silently. Pass tags=/description= to answer in advance, or
# ask=False to go back to never being prompted.
with nebula.session(ARCHIVE, artifact_tags=file_tags) as sess:

	# Here we pretend to do a measurement
	data_x = []
	data_y = []
	for i in range(20):

		# Make bogus data
		data_x.append(i+randrange(-0.1, 0.1))
		data_y.append(randrange(-1, 1))

	# Get the on-disk path to an artifact of a given name
	fn = sess.artifact_path("test.tome")

	# You can modify filenames into different extensions using `with_suffix.`
	# However, this is a feature of PathLib, not nebula. Nebula interfaces with
	# it by returning `fn` from `artifact_path` as a pathlib.PosixPath.
	log_fn = fn.with_suffix(".log")

	# NOTE: There is also:
	#    sess.artifact()
	# This is NOT what you use here; it is used in conjunction with `with`
	# to automatically write artifacts with their sidecars.

	# Note that the sess.artifact_path bit was unneccesary; it was being undone
	# by fn.name, which strips the path and just prints the filename.
	#
	# If you print `art` and `type(art)` it'll show as a pathlib.PosixPath, just
	# like the output of artifact_path. However, it is not the same. The artifact
	# function actually makes an `_ArtifactWriter` object which auto-writes the
	# sidecar and throws errors if problems occur.
	#
	# `tags=` here adds to the session-wide `artifact_tags` above, for
	# something true of this one file only.
	with sess.artifact("test.tome", tags=["sweep"]) as art:

		# Save some data. If no file is written under the produced artifact name,
		# the _ArtifactWriter will throw an error. It does not want to fail
		# silently.
		dict_to_tome({"x":data_x, "y":data_y}, art)
