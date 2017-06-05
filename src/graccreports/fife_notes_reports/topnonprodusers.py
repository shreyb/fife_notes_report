import sys
import copy
import re
import datetime
import csv
from os.path import expanduser, join

from elasticsearch_dsl import Search
import tabulate

from . import Reporter, get_configfile, Configuration
from . import TextUtils, NiceNum

MAXINT = 2**31 - 1


# Helper functions
def coroutine(func):
    """Decorator to prime coroutines by advancing them to their first yield
    point

    :param function func: Coroutine function to prime
    :return function: Coroutine that's been primed
    """
    def wrapper(*args, **kwargs):
        cr = func(*args, **kwargs)
        cr.next()
        return cr
    return wrapper


@Reporter.init_reporter_parser
def parse_opts(parser):
    """
    Specific argument parser for this report.  The decorator initializes the
    argparse.ArgumentParser object, calls this function on that object to
    modify it, and then returns the Namespace from that object.

    :param parser: argparse.ArgumentParser object that we intend to add to
    :return: None
    """
    parser.add_argument("-m", "--hourlimit", dest="hours", type=int,
                        help="Minimum number of wall hours", default=0)
    parser.add_argument("-N", "--num", dest="num", type=int,
                        help="Show these many entries", default=100)
    parser.add_argument("-C", "--cutoff", dest="cutoff", type=float,
                        help="Lower efficiency cutoff for entries (default 0.9)",
                        default=0.9)

    # Report-specific args


class TopNonprodUsers(Reporter):
    """
    Class to hold information about and to run Top Non-production users report.

    :param Configuration.Configuration config: Report Configuration object
    :param str start: Start time of report range
    :param str end: End time of report range
    :param float hour_limit: Minimum number of hours a user must have run to
    get reported on
    :param int num: Maximum number of users to report on
    :param float cutoff: Efficiency level (as a decimal) under which we don't
    report users
    :param bool is_test: Whether or not this is a test run.
    :param bool no_email: If true, don't actually send the email
    :param bool verbose: Verbose flag
    """
    def __init__(self, config, start, end, hour_limit=0, num=100, cutoff=0.9,
                 is_test=False, no_email=False,
                 verbose=False):
        report = 'topnonprod'
        Reporter.__init__(self, report, config, start, end, verbose=verbose,
                          logfile=None, no_email=no_email, is_test=is_test,
                          logfile_override=None, check_vo=False)
        self.num = num
        self.cilogon_match = re.compile('.+CN=([\w\s]+)/CN=UID:(\w+)')
        self.non_cilogon_match = re.compile('.+/CN=([\w\s]+)/?.+?')
        self.hour_limit = hour_limit
        self.cutoff = cutoff
        self.userlist = []

    def run_report(self):
        """Handles the data flow throughout the report generation.  Generates
        the raw data, the HTML report, and sends the email.

        :return None
        """
        self.generate()
        self.order_lines()
        self.print_lines()
        return

    def query(self):
        """
        Method to query Elasticsearch cluster for EfficiencyReport information

        :return elasticsearch_dsl.Search: Search object containing ES query
        """
        # Gather parameters, format them for the query
        starttimeq = self.start_time.isoformat()
        endtimeq = self.end_time.isoformat()
        wildcardProbeNameq = 'condor:fifebatch?.fnal.gov'

        if self.verbose:
            self.logger.info(self.indexpattern)

        # Elasticsearch query and aggregations
        s = Search(using=self.client, index=self.indexpattern) \
            .filter("wildcard", ProbeName=wildcardProbeNameq) \
            .filter("wildcard", RawVOName="*Analysis*") \
            .filter("range", EndTime={"gte": starttimeq, "lt": endtimeq}) \
            .filter("term", Host_description="GPGrid") \
            .filter("term", ResourceType="Payload")\
            .filter("term", Resource_ExitCode=0)[0:0]
        # Size 0 to return only aggregations

        self.unique_terms = ['DN', 'VOName']
        self.metrics = ['CpuDuration_user', 'CpuDuration_system', 'CoreHours']

        # Bucket aggs
        Bucket = s.aggs.bucket('DN', 'terms', field='DN', size=MAXINT) \
            .bucket('VOName', 'terms', field='VOName', size=MAXINT)

        # Metric aggs
        for m in self.metrics:
            Bucket.metric(m, 'sum', field=m)

        return s

    def generate(self):
        """
        Runs the ES query, checks for success, and then
        sends the raw data to parser for processing.

        :return: None
        """
        results = self.run_query()

        def recurseBucket(curData, curBucket, index, data):
            """
            Recursively process the buckets down the nested aggregations

            :param curData: Current parsed data that describes curBucket and will be copied and appended to
            :param bucket curBucket: A elasticsearch bucket object
            :param int index: Index of the unique_terms that we are processing
            :param data: list of dicts that holds results of processing

            :return: None.  But this will operate on a list *data* that's passed in and modify it
            """
            curTerm = self.unique_terms[index]

            # Check if we are at the end of the list
            if not curBucket[curTerm]['buckets']:
                # Make a copy of the data
                nowData = copy.deepcopy(curData)
                data.append(nowData)
            else:
                # Get the current key, and add it to the data
                for bucket in curBucket[curTerm]['buckets']:
                    nowData = copy.deepcopy(curData)  # Hold a copy of curData so we can pass that in to any future recursion
                    nowData[curTerm] = bucket['key']
                    if index == (len(self.unique_terms) - 1):
                        # reached the end of the unique terms
                        for metric in self.metrics:
                            nowData[metric] = bucket[metric].value
                            # Add the doc count
                        nowData["Count"] = bucket['doc_count']
                        data.append(nowData)
                    else:
                        recurseBucket(nowData, bucket, index + 1, data)

        data = []
        recurseBucket({}, results, 0, data)

        pline = self._parse_lines()

        for item in data:
            if 'CoreHours' in item and item['CoreHours'] > self.hour_limit:
                pline.send(item)

    @coroutine
    def _parse_lines(self):
        """
        Coroutine: For each dict of raw data, this gets user's name, wall hours,
        and experiment and calculates efficiency, and stores it in self.userlist
        as a dict
        """
        while True:
            rawdict = yield
            user = self._parseCN(rawdict['DN'])
            wallhrs = rawdict['CoreHours']
            eff = self._calc_eff(wallhrs,
                                 rawdict['CpuDuration_system'] + rawdict['CpuDuration_user'])
            exp = rawdict['VOName']

            self.userlist.append({'Name': user, 'WallHours': wallhrs,
                                  'Efficiency': eff, 'Experiment': exp})

    @staticmethod
    def _calc_eff(wallhours, cpusec):
        """
        Calculate the efficiency given the wall hours and cputime in
        seconds.  Returns percentage

        :param float wallhours: Wall Hours of a bucket
        :param float cpusec: CPU time (in seconds) of a bucket
        :return float: Efficiency of that bucket
        """
        return (cpusec / 3600) / wallhours

    def _parseCN(self, cn):
        """Parse the DN to grab the username

        :param str cn: DN string from record
        :return str: username as pulled from cn
        """
        m = self.cilogon_match.match(cn)  # CILogon certs
        if m:
            pass
        else:
            m = self.non_cilogon_match.match(cn)
            if not m:
                return cn
        user = m.group(1)
        return user

    def order_lines(self):
        """Sort the lines from self.userlist, and compile the table of lines
        to print"""
        finallist = sorted(self.userlist, key=lambda x: x['Efficiency'], reverse=True)

        print "The most efficient big non-production user on GPGrid " \
              "who used more than 100,000 hours for successful jobs " \
              "since {date} is {name} with {efficiency}% efficiency.\n".format(
            date="{dt:%b} {dt.day}, {dt.year}".format(dt=self.start_time),
            name=finallist[0]['Name'],
            efficiency=round(float(finallist[0]['Efficiency']) * 100, 1))

        self.header = ['Rank', 'Experiment', 'Name', 'Wall Hours', 'Efficiency']
        self.tablelist = []
        for n, d in enumerate(finallist, start=1):
            eff = round(float(d['Efficiency']) * 100, 1)
            if n > self.num or float(d['Efficiency']) < self.cutoff:
                break
            self.tablelist.append([n, d['Experiment'], d['Name'],
                                   NiceNum.niceNum(d['WallHours']),
                                   '{0}%'.format(eff)])
        return

    def print_lines(self):
        """Print the table lines and write them to CSV file"""
        print tabulate.tabulate(self.tablelist, headers=self.header)

        homedir = expanduser('~')
        fname = 'topnonprodusers.csv'
        pth = join(homedir, fname)

        with open(pth, 'w') as csvfile:
            wr = csv.writer(csvfile, delimiter=',')
            wr.writerow(self.header)
            for row in self.tablelist:
                wr.writerow(row)
        return


def main():
    args = parse_opts()

    # # Set up the configuration
    config = Configuration.Configuration()
    config.configure(get_configfile(override=args.config, flag='fifenotes'))

    try:

        T = TopNonprodUsers(config,
                            args.start,
                            args.end,
                            args.hours,
                            args.num,
                            args.cutoff,
                            args.is_test,
                            args.no_email,
                            args.verbose)
        T.run_report()
    except Exception as e:
        errstring = '{0}: Error running "Top Non-production users" report'.format(
            datetime.datetime.now())
        print e, errstring
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
