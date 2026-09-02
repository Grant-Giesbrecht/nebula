from nebula.cli import AUTO_INTAKE_NICKNAME
import nebula
from stardust.algorithm import randrange
from stardust.tome import dict_to_tome
import pylogfile as plf

log = plf.LogPile()

ARCHIVE = AUTO_INTAKE_NICKNAME
nebula.validate_archive(ARCHIVE)

# Get tags from user for the ARTIFACTS produced by this run
art_tags = nebula.input_tag(ARCHIVE)

# If a new session is created, it will ask for tags and a description. Otehrwise
# it will just go ahead and rip.
with nebula.session(ARCHIVE, artifact_tags=art_tags) as sess:
	
	# Here we pretend to do a measurement
	data_x = []
	data_y = []
	for i in range(20):

		# Make bogus data
		data_x.append(i+randrange(-0.1, 0.1))
		data_y.append(randrange(-1, 1))
		
		x_ = data_x[-1]
		y_ = data_y[-1]
		plf.info(f"Added x={x_}, y={y_}")
	
	# Write one artifact
	with sess.artifact("test.tome", tags="bulk_data") as art:
		dict_to_tome({"x":data_x, "y":data_y}, art)
	
	with sess.artifact("test.pylog", tags) as art:
		log.save_plflog(art)
	