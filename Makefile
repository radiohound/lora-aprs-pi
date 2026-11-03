# Builds the LACF codec library and runs the tests.
#   make          build lacf/liblacf.so
#   make test     build, then run every test
#   make run      start the station in simulate mode (no radio needed)

CC     ?= gcc
CFLAGS ?= -O2 -fPIC

LACF_SRC = lacf/hb.c lacf/hbi.c lacf/hbc.c
LACF_DEP = $(LACF_SRC) lacf/hb.h lacf/hb_payload.h lacf/aprs_ppm.c lacf/aprs_ppm.h \
           lacf/aprs_ppm_model.h lacf/model_hybridB_info_native.c lacf/model_hybridB_comment.c

all: lacf/liblacf.so

lacf/liblacf.so: $(LACF_DEP)
	$(CC) $(CFLAGS) -shared -Ilacf -o $@ $(LACF_SRC)

test: lacf/liblacf.so
	python3 -m unittest discover -s tests -t . -v

run: lacf/liblacf.so
	python3 -m lora_aprs_pi --simulate --config ./dev-config.json --port 8080

clean:
	rm -f lacf/liblacf.so

.PHONY: all test run clean
