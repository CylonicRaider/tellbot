
.NOTPARALLEL:
.PHONY: update

MANUAL.pdf: USAGE.md
	pandoc $< --filter ./headings.py -V geometry=margin=1.5in -o $@

# Yay! Plumbing commands!
update:
	git checkout master -- USAGE.md
	$(MAKE) MANUAL.pdf
	git add MANUAL.pdf
	git update-ref HEAD $$(git commit-tree \
	    -m "Import changes from 'master'" \
	    -p $$(git rev-parse HEAD) -p $$(git rev-parse master) \
	    $$(git write-tree))