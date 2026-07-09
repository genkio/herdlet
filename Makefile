test:
	python3 -m unittest discover -s tests -v

install:
	install -m 0755 herdlet.py /usr/local/bin/herdlet

.PHONY: test install
