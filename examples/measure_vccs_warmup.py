from constellation.all import *
from inputimeout import inputimeout, TimeoutOccurred
from stardust.sandbox import dict_to_tome
from stardust.algorithm import randrange
from pathlib import Path
import nebula

ARCHIVE = "test-archive"

log = plf.LogPile()

# dmm = SiglentSDM3000X("TCPIP0::192.168.1.30::INSTR", log)
# if not dmm.online:
# 	log.critical(f"DMM failed to connect.")
# 	exit()
# else:
# 	log.info(f"DMM online.")
# 
# # Configure DMM
# dmm.set_measurement(DigitalMultimeter.MEAS_CURR_DC)
# dmm.set_trigger_type(DigitalMultimeter.TRIG_SINGLE)

user_notes = input("What is the set voltage? how long has the DUT been powered on?: ")

# input_tag() is like input() but tag-aware: the user can /list and
# /search the tags already in this archive (and TAB-complete them), so
# they reuse "warmup" instead of inventing "warm-up" / "warm_up". Press
# Enter on an empty line when done.
tags = nebula.input_tag(ARCHIVE)

# nebula.session() with no run_id pops an interactive picker: it lists the
# sessions you could append to (opened today, or still OPEN from before) so
# you can add to a run in progress instead of spraying data across many
# one-shot folders -- or type /new to start fresh. Pass new_session=True to
# skip the prompt and always start clean, or run_id="S-0123" to append to a
# specific one. It closes the session on normal exit and marks it "crashed"
# if an exception propagates out of the with-block, so a script you Ctrl+C
# out of leaves an honest record rather than a folder claiming to be done.
with nebula.session(ARCHIVE, tags=tags, description="VCCS warm-up measurement") as s:

	fn = s.artifact_path("vccs_warm_up.tome")
	lfn = fn.with_suffix(".log")

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
			
			# dmm.send_manual_trigger()
			# val = dmm.get_value()
			val = randrange(0, 3)
			ts = datetime.datetime.now()
			tss = str(ts)
			
			log.info(f"Measured value of {val} A.")
			
			data['timestamps'].append(tss)
			data['t_s'].append((ts-t0).total_seconds())
			data['dc_current_A'].append(val)
		
		
			
	# s.artifact() is the safe way to save: it hands you the path to write
	# to, then writes the sidecar for you when the with-block exits -- so
	# there's no separate write_meta_for() call to forget, and the inputs
	# stay next to the code that knows them. The sidecar records which
	# commit of which repo produced the file (captured automatically from
	# this script's git state) plus the structured inputs below.
	log.info(f"Saving data to file: {fn}")
	with s.artifact(
		fn.name,
		inputs={"user_notes": user_notes, "program_notes":"This is dummy data generated to test nebula, it is NOT real measured data."},
	) as data_path:
		dict_to_tome(data, data_path)

	# The log is a separate artifact derived from the same acquisition.
	log.info(f'Saving log to file: {lfn}')
	with s.artifact(lfn.name, derived_from=[fn.name]) as log_path:
		log.save_plflog(log_path)

	# Anything you write with plain artifact_path() and forget to document
	# still gets caught: on clean close the session auto-writes a
	# provenance-only sidecar for any orphan file AND prints a warning
	# naming it (the default on_missing_meta="stub+warn" policy), so nothing
	# lands in the archive untracked and you still hear about the missing
	# inputs. Pass on_missing_meta="raise" to nebula.session() if you'd
	# rather the script fail outright, or "stub" to stub silently.
	print(f"Session: {s.id}  ({s.path})")
