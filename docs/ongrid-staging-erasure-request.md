# Request to OnGrid: erase the staging community

Real candidates' identity documents were uploaded to the **staging** community
(79355) while the integration was being built: Aadhaar, PAN, address proof,
education certificates and cancelled cheques, belonging to actual people who
applied to Optiminastic.

Circle can unlink its own records (`scripts/clear_ongrid_links.py`), but the
documents live on OnGrid's side and their API exposes no deletion - the client
in `app/services/ongrid.py` has create, upload, status, report and
request_check, and nothing that removes anything. Only OnGrid can do this.

Send once production is confirmed working, so nothing still in use is erased.

---

**To:** the OnGrid account contact
**Subject:** Request to erase test data in staging community 79355

Hello,

We have completed our integration and moved to production (community 187363).

During the build we created individuals in the **staging community 79355** using
the real details and documents of candidates who had applied to us - Aadhaar,
PAN, address proof, education certificates and bank documents. That data was
only ever meant for testing, and it should not remain in a non-production
environment.

Could you please **permanently erase every individual and uploaded document in
community 79355**, and confirm in writing once it is done? If you need us to
list the individual IDs rather than clearing the whole community, tell us and
we will send them.

We would also like to know:

1. Whether any of those documents were copied to backups or logs that the
   erasure will not cover, and how long those are retained.
2. Whether verifications that ran in staging against real documents were shared
   with any third party or data source.

We are asking because these are real individuals' government IDs, and our
retention commitment to them does not extend to a test environment.

Thank you,
Optiminastic
