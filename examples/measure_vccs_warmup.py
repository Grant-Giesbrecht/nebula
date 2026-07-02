from constellation.all import *
from inputimeout import inputimeout, TimeoutOccurred
from stardust.sandbox import dict_to_tome
from pathlib import Path
import nebula

# TODO: point this at your actual store root (NAS path for postdoc data).
# This can also just live in a shared constants module you import instead
# of repeating it at the top of every script.
STORE_ROOT = Path("/nas/nist-data")

log = plf.LogPile()

dmm = SiglentSDM3000X("TCPIP0::192.168.1.30::INSTR", log)
if not dmm.online:
	log.critical(f"DMM failed to connect.")
	exit()
else:
	log.info(f"DMM online.")

# Configure DMM
dmm.set_measurement(DigitalMultimeter.MEAS_CURR_DC)
dmm.set_trigger_type(DigitalMultimeter.TRIG_SINGLE)

user_notes = input("What is the set voltage? how long has the DUT been powered on?: ")
tags_input = input("Tags (comma-separated, optional): ")
tags = [t.strip() for t in tags_input.split(",") if t.strip()]

# nebula.session() creates a new S-XXXX folder (or, if you pass
# run_id=..., appends to one you're already partway through). It closes
# the session on normal exit and marks it "crashed" if an exception
# propagates out of the with-block -- so a script you Ctrl+C out of
# leaves an honest record rather than a folder claiming to be done.
with nebula.session(STORE_ROOT, tags=tags, description="VCCS warm-up measurement") as s:

	fn = s.artifact_path("vccs_warm_up.tome")

	t0 = datetime.datetime.now()
	data = {"user_notes":user_notes, "timestamps":[], "dc_current_A": [], "t_s":[], "start_time":str(t0)}

	running = True
	while running:
		
		try:
			user_input = inputimeout(prompt='(EXIT to stop): ', timeout=5)
			log.debug(f"User input = {user_input}")
			
			if user_input == "EXIT":
				running = False
		except TimeoutOccurred:
			log.debug(f"No user input provided. Logging datapoint.")
			
			dmm.send_manual_trigger()
			val = dmm.get_value()
			ts = datetime.datetime.now()
			tss = str(ts)
			
			log.info(f"Measured value of {val} {dmm.check_units}.")
			
			data['timestamps'].append(tss)
			data['t_s'].append((ts-t0).total_seconds())
			data['dc_current_A'].append(val)
		
		
			
	log.info(f"Saving data to file: {fn}")
	dict_to_tome(data, fn)

	lfn = fn.with_suffix(".log")
	log.info(f'Saving log to file: {lfn}')
	log.save_plflog(lfn)

	# Sidecar for the data file: records which commit of which repo
	# produced it (captured automatically from this script's git state),
	# plus the set-voltage/warm-up note as structured input rather than
	# just free text buried in the .tome.
	s.write_meta_for(
		fn.name,
		inputs={"user_notes": user_notes},
	)
	# The log is a separate artifact derived from the same acquisition,
	# not raw data itself -- worth its own (small) sidecar so it doesn't
	# look like an orphan file if you ever browse the session later.
	s.write_meta_for(lfn.name, derived_from=[fn.name])

	print(f"Session: {s.id}  ({s.path})")