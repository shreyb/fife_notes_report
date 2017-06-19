FIFE NOTES Reports
=====================================

For each report, you can specify a non-standard location for the config file with -c, or for a template file with -T.  The defaults are in [src/graccreports/config](https://github.com/shreyb/fife_notes_report/tree/master/src/graccreports/config) and [src/graccreports/html_templates](https://github.com/shreyb/fife_notes_report/tree/master/src/graccreports/html_templates).
The -d, -n, and -v flags are, respectively, dryrun (test), no email, and verbose.

Examples:


**Top Non-production users report**
```
topnonprodusersreport -v -d -n -s 2017-04-01 -e 2017-05-31 -N 10 -m 100000
```


Change the date/times, and the VO where applicable.  -v makes it verbose.
