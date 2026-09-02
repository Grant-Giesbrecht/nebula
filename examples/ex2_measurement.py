from nebula.cli import AUTO_INTAKE_NICKNAME
import nebula
from stardust.algorithm import randrange
from stardust.tome import dict_to_tome

# Set archive to auto-intake and validate
ARCHIVE = AUTO_INTAKE_NICKNAME
nebula.validate_archive(ARCHIVE)

# Session tags are tags that will be attached to new sesisons 
# created using the `nebula.session(...)` function. You can manually create a
# a list of tags as shown below, or you can input them from the picker interface.
# The picker is recommended for applications where the tags are selected at run-
# time because it allows new (or mistyped) tags to be flagged, in addition to
# listing existing tags.
#  
session_tags = ["nebula", "from_intake"] # Tags need to be just a list of (valid) strings
session_tags = nebula.input_tag(ARCHIVE) # Run the picker

session_description = "This is a session made by an example script."

with nebula.session(ARCHIVE, tags=session_tags, description=session_description) as sess:
	
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
	with sess.artifact("test.tome") as art:
		
		# Save some data. If no file is written under the produced artifact name,
		# the _ArtifactWriter will throw an error. It does not want to fail 
		# silently.
		dict_to_tome({"x":data_x, "y":data_y}, art)