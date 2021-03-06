
from csv import DictWriter
from datetime import datetime, timedelta
from dateutil.parser import parse as parse_date
from io import StringIO
import http.client
import json
import math
import re
import time

import requests
import lz4framed

import sentry_sdk as sentry

from flask import current_app, request, make_response, abort
from flask.json import jsonify
from werkzeug.exceptions import HTTPException, BadRequest

from sqlalchemy import func, and_, false, text, select, sql, column
from sqlalchemy.sql import literal_column
from sqlalchemy import String, cast
from sqlalchemy.exc import OperationalError
from psycopg2.extensions import QueryCanceledError

from urllib.parse import urljoin, urlencode

from measurements import __version__
from measurements.config import REPORT_INDEX_OFFSET, REQID_HDR, request_id

MSM_ID_PREFIX = "temp-id"
FASTPATH_MSM_ID_PREFIX = "temp-fid-"
RE_MSM_ID = re.compile("^{}-(\d+)$".format(MSM_ID_PREFIX))
FASTPATH_SERVER = "fastpath.ooni.nu"
FASTPATH_PORT = 8000


class QueryTimeoutError(HTTPException):
    code = 504
    description = "The database query timed out.\nTry changing the query parameters."


def get_version():
    return jsonify({"version": __version__})


def list_files(
    probe_asn=None,
    probe_cc=None,
    test_name=None,
    since=None,
    until=None,
    since_index=None,
    order_by="index",
    order="desc",
    offset=0,
    limit=100,
):
    log = current_app.logger

    if probe_asn is not None:
        if probe_asn.startswith("AS"):
            probe_asn = probe_asn[2:]
        probe_asn = int(probe_asn)

    try:
        if since is not None:
            since = parse_date(since)
    except ValueError:
        raise BadRequest("Invalid since")

    try:
        if until is not None:
            until = parse_date(until)
    except ValueError:
        raise BadRequest("Invalid until")

    if since_index is not None:
        since_index = int(since_index)
        report_no = max(0, since_index - REPORT_INDEX_OFFSET)

    if order_by in ("index", "idx"):
        order_by = "report_no"

    cols = [
        literal_column("textname"),
        literal_column("test_start_time"),
        literal_column("probe_cc"),
        literal_column("probe_asn"),
        literal_column("report_no"),
        literal_column("test_name"),
    ]
    where = []
    query_params = {}

    # XXX maybe all of this can go into some sort of function.
    if probe_cc:
        where.append(sql.text("probe_cc = :probe_cc"))
        query_params["probe_cc"] = probe_cc

    if probe_asn:
        where.append(sql.text("probe_asn = :probe_asn"))
        query_params["probe_asn"] = probe_asn

    if test_name:
        where.append(sql.text("test_name = :test_name"))
        query_params["test_name"] = test_name

    if since:
        where.append(sql.text("test_start_time > :since"))
        query_params["since"] = since

    if until:
        where.append(sql.text("test_start_time <= :until"))
        query_params["until"] = until

    if since_index:
        where.append(sql.text("report_no > :report_no"))
        query_params["report_no"] = report_no

    query = select(cols).where(and_(*where)).select_from("report")
    count = -1
    pages = -1
    current_page = math.ceil(offset / limit) + 1

    query = query.order_by(text("{} {}".format(order_by, order)))
    query = query.limit(limit).offset(offset)

    results = []

    log.debug(query)
    q = current_app.db_session.execute(query, query_params)
    for row in q:
        download_url = urljoin(
            current_app.config["BASE_URL"], "/files/download/%s" % row.textname
        )
        results.append(
            {
                "download_url": download_url,
                "probe_cc": row.probe_cc,
                "probe_asn": "AS{}".format(row.probe_asn),
                "test_name": row.test_name,
                "index": int(row.report_no) + REPORT_INDEX_OFFSET,
                "test_start_time": row.test_start_time,
            }
        )
    # We got less results than what we expected, we know the count and that we are done
    if len(results) < limit:
        count = offset + len(results)
        pages = math.ceil(count / limit)
        next_url = None
    else:
        next_args = request.args.to_dict()
        next_args["offset"] = "%s" % (offset + limit)
        next_args["limit"] = "%s" % limit
        next_url = urljoin(
            current_app.config["BASE_URL"], "/api/v1/files?%s" % urlencode(next_args)
        )

    metadata = {
        "offset": offset,
        "limit": limit,
        "count": count,
        "pages": pages,
        "current_page": current_page,
        "next_url": next_url,
    }

    return jsonify({"metadata": metadata, "results": results})


def get_one_fastpath_measurement(measurement_id, download):
    """Get one measurement from the fastpath table by measurement_id,
    fetching the file from the fastpath host
    """
    log = current_app.logger
    tid = measurement_id[len(FASTPATH_MSM_ID_PREFIX) :]

    path = "/measurements/{}.json.lz4".format(tid)
    log.info(
        "Incoming fastpath query %r. Fetching %s:%d%s",
        measurement_id,
        FASTPATH_SERVER,
        FASTPATH_PORT,
        path,
    )
    conn = http.client.HTTPConnection(FASTPATH_SERVER, FASTPATH_PORT)
    log.debug("Fetching %s:%d %r", FASTPATH_SERVER, FASTPATH_PORT, path)
    conn.request("GET", path)
    r = conn.getresponse()
    log.debug("Response status: %d", r.status)
    try:
        assert r.status == 200
        blob = r.read()
        conn.close()
        log.debug("Decompressing LZ4 data")
        blob = lz4framed.decompress(blob)
        response = make_response(blob)
        response.headers.set("Content-Type", "application/json")
        log.debug("Sending JSON response")
        return response
    except Exception:
        raise BadRequest("No measurement found")


def get_measurement(measurement_id, download=None):
    """Get one measurement by measurement_id,
    fetching the file from S3 or the fastpath host as needed
    Returns only the measurement without extra data from the database
    """
    if measurement_id.startswith(FASTPATH_MSM_ID_PREFIX):
        return get_one_fastpath_measurement(measurement_id, download)

    # XXX this query is slow due to filtering by report_id and input
    # It also occasionally return multiple rows and serves only the first one
    # TODO: add timing metric
    # TODO: switch to OOID to speed up the query
    # https://github.com/ooni/pipeline/issues/48
    m = RE_MSM_ID.match(measurement_id)
    if not m:
        raise BadRequest("Invalid measurement_id")
    msm_no = int(m.group(1))

    cols = [
        literal_column("measurement.report_no"),
        literal_column("frame_off"),
        literal_column("frame_size"),
        literal_column("intra_off"),
        literal_column("intra_size"),
        literal_column("textname"),
        literal_column("report.autoclaved_no"),
        literal_column("autoclaved.filename"),
    ]
    table = (
        sql.table("measurement")
        .join(
            sql.table("report"), sql.text("measurement.report_no = report.report_no"),
        )
        .join(
            sql.table("autoclaved"),
            sql.text("autoclaved.autoclaved_no = report.autoclaved_no"),
        )
    )
    where = sql.text("measurement.msm_no = :msm_no")
    query = select(cols).where(where).select_from(table)
    query_params = dict(msm_no=msm_no)
    q = current_app.db_session.execute(query, query_params)

    msmt = q.fetchone()
    if msmt is None:
        abort(404)

    # Usual size of LZ4 frames is 256kb of decompressed text.
    # Largest size of LZ4 frame was ~55Mb compressed and ~56Mb decompressed. :-/
    range_header = "bytes={}-{}".format(
        msmt.frame_off, msmt.frame_off + msmt.frame_size - 1
    )
    filename = msmt["autoclaved.filename"]
    r = requests.get(
        urljoin(current_app.config["AUTOCLAVED_BASE_URL"], filename),
        headers={"Range": range_header, REQID_HDR: request_id()},
    )
    r.raise_for_status()
    blob = r.content
    if len(blob) != msmt.frame_size:
        raise RuntimeError("Failed to fetch LZ4 frame", len(blob), msmt.frame_size)
    blob = lz4framed.decompress(blob)[msmt.intra_off : msmt.intra_off + msmt.intra_size]
    if len(blob) != msmt.intra_size or blob[:1] != b"{" or blob[-1:] != b"}":
        raise RuntimeError(
            "Failed to decompress LZ4 frame to measurement.json",
            len(blob),
            msmt.intra_size,
            blob[:1],
            blob[-1:],
        )
    # There is no replacement of `measurement_id` with `msm_no` or anything
    # else to keep sanity. Maybe it'll happen as part of orchestration update.
    # Also, blob is not decoded intentionally to save CPU
    filename = "ooni-msmt-{}-{}".format(measurement_id, msmt.textname.replace("/", "-"))
    response = make_response(blob)
    response.headers.set("Content-Type", "application/json")
    if download is not None:
        response.headers.set("Content-Disposition", "attachment", filename=filename)
    return response


def _merge_results(tmpresults):
    """Trim list_measurements() outputs that share the same report_id/input
    """
    resultsmap = {}
    for r in tmpresults:
        k = (r["report_id"], r["input"])
        if k not in resultsmap:
            resultsmap[k] = r

    return tuple(resultsmap.values())


def list_measurements(
    report_id=None,
    probe_asn=None,
    probe_cc=None,
    test_name=None,
    since=None,
    until=None,
    since_index=None,
    order_by=None,
    order="desc",
    offset=0,
    limit=100,
    failure=None,
    anomaly=None,
    confirmed=None,
    category_code=None,
):
    """Search for measurements using only the database. Provide pagination.
    """
    # TODO: list_measurements and get_measurement will be simplified and
    # made faster by OOID: https://github.com/ooni/pipeline/issues/48

    log = current_app.logger

    ## Workaround for https://github.com/ooni/probe/issues/1034
    user_agent = request.headers.get("User-Agent")
    if user_agent.startswith("okhttp"):
        bug_probe1034_response = jsonify(
            {
                "metadata": {
                    "count": 1,
                    "current_page": 1,
                    "limit": 100,
                    "next_url": None,
                    "offset": 0,
                    "pages": 1,
                    "query_time": 0.001,
                },
                "results": [{"measurement_url": ""}],
            }
        )
        return bug_probe1034_response

    ## Prepare query parameters

    input_ = request.args.get("input")
    domain = request.args.get("domain")

    if probe_asn is not None:
        if probe_asn.startswith("AS"):
            probe_asn = probe_asn[2:]
        probe_asn = int(probe_asn)

    # When the user specifies a list that includes all the possible values for
    # boolean arguments, that is logically the same of applying no filtering at
    # all.
    # TODO: treat it as an error?
    if failure is not None:
        if set(failure) == set(["true", "false"]):
            failure = None
        else:
            failure = set(failure) == set(["true"])
    if anomaly is not None:
        if set(anomaly) == set(["true", "false"]):
            anomaly = None
        else:
            anomaly = set(anomaly) == set(["true"])
    if confirmed is not None:
        if set(confirmed) == set(["true", "false"]):
            confirmed = None
        else:
            confirmed = set(confirmed) == set(["true"])

    try:
        if since is not None:
            since = parse_date(since)
    except ValueError:
        raise BadRequest("Invalid since")

    try:
        if until is not None:
            until = parse_date(until)
    except ValueError:
        raise BadRequest("Invalid until")

    if order.lower() not in ("asc", "desc"):
        raise BadRequest("Invalid order")

    INULL = ""  # Special value for input = NULL to merge rows with FULL OUTER JOIN

    ## Create measurement+report colums for SQL query
    cols = [
        # sql.text("measurement.input_no"),
        literal_column("report.test_start_time").label("test_start_time"),
        literal_column("measurement.measurement_start_time").label(
            "measurement_start_time"
        ),
        func.concat(MSM_ID_PREFIX, "-", sql.text("measurement.msm_no")).label(
            "measurement_id"
        ),
        literal_column("measurement.report_no").label("m_report_no"),
        func.coalesce(sql.text("measurement.anomaly"), false()).label("anomaly"),
        func.coalesce(sql.text("measurement.confirmed"), false()).label("confirmed"),
        sql.text("measurement.exc IS NOT NULL AS failure"),
        func.coalesce("{}").label("scores"),
        literal_column("measurement.exc").label("exc"),
        literal_column("measurement.residual_no").label("residual_no"),
        literal_column("report.report_id").label("report_id"),
        literal_column("report.probe_cc").label("probe_cc"),
        literal_column("report.probe_asn").label("probe_asn"),
        literal_column("report.test_name").label("test_name"),
        literal_column("report.report_no").label("report_no"),
        func.coalesce(sql.text("domain_input.input"), INULL).label("input"),
    ]

    ## Create fastpath columns for query
    fpcols = [
        # func.coalesce(0).label("m_input_no"),
        # We use test_start_time here as the batch pipeline has many NULL measurement_start_times
        literal_column("measurement_start_time").label("test_start_time"),
        literal_column("measurement_start_time").label("measurement_start_time"),
        func.concat(FASTPATH_MSM_ID_PREFIX, sql.text("tid")).label("measurement_id"),
        literal_column("anomaly").label("anomaly"),
        literal_column("confirmed").label("confirmed"),
        literal_column("msm_failure").label("failure"),
        cast(sql.text("scores"), String).label("scores"),
        literal_column("report_id"),
        literal_column("probe_cc"),
        literal_column("probe_asn"),
        literal_column("test_name"),
        func.coalesce(sql.text("fastpath.input"), INULL).label("input"),
    ]

    mrwhere = []
    fpwhere = []
    query_params = {}

    # Populate WHERE clauses and query_params dict

    if since is not None:
        query_params["since"] = since
        mrwhere.append(sql.text("measurement.measurement_start_time > :since"))
        fpwhere.append(sql.text("measurement_start_time > :since"))

    if until is not None:
        query_params["until"] = until
        mrwhere.append(sql.text("measurement.measurement_start_time <= :until"))
        fpwhere.append(sql.text("measurement_start_time <= :until"))

    if report_id:
        query_params["report_id"] = report_id
        mrwhere.append(sql.text("report.report_id = :report_id"))
        fpwhere.append(sql.text("report_id = :report_id"))

    if probe_cc:
        query_params["probe_cc"] = probe_cc
        mrwhere.append(sql.text("report.probe_cc = :probe_cc"))
        fpwhere.append(sql.text("probe_cc = :probe_cc"))

    if probe_asn is not None:
        query_params["probe_asn"] = probe_asn
        mrwhere.append(sql.text("report.probe_asn = :probe_asn"))
        fpwhere.append(sql.text("probe_asn = :probe_asn"))

    if test_name is not None:
        query_params["test_name"] = test_name
        mrwhere.append(sql.text("report.test_name = :test_name"))
        fpwhere.append(sql.text("test_name = :test_name"))

    # Filter on anomaly, confirmed and failure:
    # The database stores anomaly and confirmed as boolean + NULL and stores
    # failures in different columns. This leads to many possible combinations
    # but only a subset is used.
    # On anomaly and confirmed: any value != TRUE is treated as FALSE
    # See test_list_measurements_filter_flags_fastpath

    if anomaly is True:
        mrwhere.append(sql.text("measurement.anomaly IS TRUE"))
        fpwhere.append(sql.text("fastpath.anomaly IS TRUE"))

    elif anomaly is False:
        mrwhere.append(sql.text("measurement.anomaly IS NOT TRUE"))
        fpwhere.append(sql.text("fastpath.anomaly IS NOT TRUE"))

    if confirmed is True:
        mrwhere.append(sql.text("measurement.confirmed IS TRUE"))
        fpwhere.append(sql.text("fastpath.confirmed IS TRUE"))

    elif confirmed is False:
        mrwhere.append(sql.text("measurement.confirmed IS NOT TRUE"))
        fpwhere.append(sql.text("fastpath.confirmed IS NOT TRUE"))

    if failure is True:
        # residual_no is never NULL, msm_failure is always NULL
        mrwhere.append(sql.text("measurement.exc IS NOT NULL"))
        fpwhere.append(sql.text("fastpath.msm_failure IS TRUE"))

    elif failure is False:
        # on success measurement.exc is NULL
        mrwhere.append(sql.text("measurement.exc IS NULL"))
        fpwhere.append(sql.text("fastpath.msm_failure IS NOT TRUE"))

    fpq_table = sql.table("fastpath")
    mr_table = sql.table("measurement").join(
        sql.table("report"), sql.text("measurement.report_no = report.report_no"),
    )

    if input_ or domain or category_code:
        # join in domain_input
        mr_table = mr_table.join(
            sql.table("domain_input"),
            sql.text("domain_input.input_no = measurement.input_no"),
        )
        fpq_table = fpq_table.join(
            sql.table("domain_input"), sql.text("domain_input.input = fastpath.input")
        )

        if input_:
            # input_ overrides domain and category_code
            query_params["input"] = input_
            mrwhere.append(sql.text("domain_input.input = :input"))
            fpwhere.append(sql.text("domain_input.input = :input"))

        else:
            # both domain and category_code can be set at the same time
            if domain:
                query_params["domain"] = domain
                mrwhere.append(sql.text("domain_input.domain = :domain"))
                fpwhere.append(sql.text("domain_input.domain = :domain"))

            if category_code:
                query_params["category_code"] = category_code
                mr_table = mr_table.join(
                    sql.table("citizenlab"),
                    sql.text("citizenlab.url = domain_input.input"),
                )
                fpq_table = fpq_table.join(
                    sql.table("citizenlab"),
                    sql.text("citizenlab.url = domain_input.input"),
                )
                mrwhere.append(sql.text("citizenlab.category_code = :category_code"))
                fpwhere.append(sql.text("citizenlab.category_code = :category_code"))

    else:
        mr_table = mr_table.outerjoin(
            sql.table("domain_input"),
            sql.text("domain_input.input_no = measurement.input_no"),
        )

    # We runs SELECTs on the measurement-report (mr) tables and faspath independently
    # from each other and then merge them.
    # The FULL OUTER JOIN query is using LIMIT and OFFSET based on the
    # list_measurements arguments. To speed up the two nested queries,
    # an ORDER BY + LIMIT on "limit+offset" is applied in each of them to trim
    # away rows that would be removed anyways by the outer query.
    #
    # During a merge we can find that a measurement is:
    # - only in fastpath:       get_measurement will pick the JSON msmt from the fastpath host
    # - in both selects:        pick `scores` from fastpath and the msmt from the can
    # - only in "mr":           the msmt from the can
    #
    # This implements a failover mechanism where new msmts are loaded from fastpath
    # but can fall back to the traditional pipeline.

    mr_query = (
        select(cols).where(and_(*mrwhere)).select_from(mr_table).limit(offset + limit)
    )
    fp_query = (
        select(fpcols)
        .where(and_(*fpwhere))
        .select_from(fpq_table)
        .limit(offset + limit)
    )

    if order_by is None:
        # Use test_start_time or measurement_start_time depending on other
        # filters in order to avoid heavy joins.
        # Filtering on anomaly, confirmed, msm_failure -> measurement_start_time
        # Filtering on probe_cc, probe_asn, test_name -> test_start_time
        # See test_list_measurements_slow_order_by_* tests
        if probe_cc or probe_asn or test_name:
            order_by = "test_start_time"
        elif anomaly or confirmed or failure or input_ or domain or category_code:
            order_by = "measurement_start_time"
        else:
            order_by = "measurement_start_time"

    mr_query = mr_query.order_by(text("{} {}".format(order_by, order)))
    fp_query = fp_query.order_by(text("{} {}".format(order_by, order)))

    mr_query = mr_query.alias("mr")
    fp_query = fp_query.alias("fp")

    j = fp_query.join(
        mr_query,
        sql.text("fp.input = mr.input AND fp.report_id = mr.report_id"),
        full=True,
    )

    def coal(colname):
        return func.coalesce(
            literal_column(f"fp.{colname}"), literal_column(f"mr.{colname}")
        ).label(colname)

    # Merge data from mr_table and fastpath.
    # Most of the time we prefer data from fastpath, using coal().
    # For measurement_id, we prefer mr_table. See test_list_measurements_shared
    merger = [
        coal("test_start_time"),
        coal("measurement_start_time"),
        func.coalesce(
            literal_column("mr.measurement_id"), literal_column("fp.measurement_id")
        ).label("measurement_id"),
        func.coalesce(literal_column("mr.m_report_no"), 0).label("m_report_no"),
        coal("anomaly"),
        coal("confirmed"),
        coal("failure"),
        func.coalesce(literal_column("fp.scores"), "{}").label("scores"),
        column("exc"),
        func.coalesce(literal_column("mr.residual_no"), 0).label("residual_no"),
        coal("report_id"),
        coal("probe_cc"),
        coal("probe_asn"),
        coal("test_name"),
        coal("input"),
    ]
    # Assemble the "external" query. Run a final order by followed by limit and
    # offset
    fob = text("{} {}".format(order_by, order))
    query = select(merger).select_from(j).order_by(fob).offset(offset).limit(limit)

    with sentry.configure_scope() as scope:
        # Set query (without params) in Sentry scope for the rest of the API call
        # https://github.com/getsentry/sentry-python/issues/184
        scope.set_extra("sql_query", query)

    # Run the query, generate the results list
    iter_start_time = time.time()

    try:
        q = current_app.db_session.execute(query, query_params)
        tmpresults = []
        for row in q:
            url = urljoin(
                current_app.config["BASE_URL"],
                "/api/v1/measurement/%s" % row.measurement_id,
            )
            tmpresults.append(
                {
                    "measurement_url": url,
                    "measurement_id": row.measurement_id,
                    "report_id": row.report_id,
                    "probe_cc": row.probe_cc,
                    "probe_asn": "AS{}".format(row.probe_asn),
                    "test_name": row.test_name,
                    "measurement_start_time": row.measurement_start_time,
                    "input": row.input,
                    "anomaly": row.anomaly,
                    "confirmed": row.confirmed,
                    "failure": row.failure,
                    "scores": json.loads(row.scores),
                }
            )
    except OperationalError as exc:
        if isinstance(exc.orig, QueryCanceledError):
            # Timeout due to a slow query. Generate metric and do not feed it
            # to Sentry.
            abort(504)

        raise exc

    # For each report_id / input tuple, we want at most one entry. Measurements
    # from mr_table and fastpath has already been merged by the FULL OUTER JOIN
    # but we have duplicate msmts sharing the same report_id / input.
    results = _merge_results(tmpresults)

    # Replace the special value INULL for "input" with None
    for i, r in enumerate(results):
        if r["input"] == INULL:
            results[i]["input"] = None

    pages = -1
    count = -1
    current_page = math.ceil(offset / limit) + 1

    # We got less results than what we expected, we know the count and that we are done
    if len(results) < limit:
        count = offset + len(results)
        pages = math.ceil(count / limit)
        next_url = None
    else:
        # XXX this is too intensive. find a workaround
        # count_start_time = time.time()
        # count = q.count()
        # pages = math.ceil(count / limit)
        # current_page = math.ceil(offset / limit) + 1
        # query_time += time.time() - count_start_time
        next_args = request.args.to_dict()
        next_args["offset"] = "%s" % (offset + limit)
        next_args["limit"] = "%s" % limit
        next_url = urljoin(
            current_app.config["BASE_URL"],
            "/api/v1/measurements?%s" % urlencode(next_args),
        )

    query_time = time.time() - iter_start_time
    metadata = {
        "offset": offset,
        "limit": limit,
        "count": count,
        "pages": pages,
        "current_page": current_page,
        "next_url": next_url,
        "query_time": query_time,
    }

    return jsonify({"metadata": metadata, "results": results[:limit]})


def _convert_to_csv(r) -> str:
    """Convert aggregation result dict/list to CSV
    """
    csvf = StringIO()
    if isinstance(r, dict):
        # 0-dimensional data
        fieldnames = sorted(r.keys())
        writer = DictWriter(csvf, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(r)

    else:
        fieldnames = sorted(r[0].keys())
        writer = DictWriter(csvf, fieldnames=fieldnames)
        writer.writeheader()
        for row in r:
            writer.writerow(row)

    result = csvf.getvalue()
    csvf.close()
    return result


def get_aggregated(
    axis_x=None,
    axis_y=None,
    category_code=None,
    domain=None,
    input=None,
    test_name=None,
    probe_asn=None,
    probe_cc=None,
    since=None,
    until=None,
    format="JSON",
):
    """Aggregate counters data
    """
    log = current_app.logger

    dimension_cnt = int(bool(axis_x)) + int(bool(axis_y))

    cacheable = until and parse_date(until) < datetime.now() - timedelta(hours=72)

    # Assemble query
    def coalsum(name):
        return sql.text("COALESCE(SUM({0}), 0) AS {0}".format(name))

    cols = [
        coalsum("anomaly_count"),
        coalsum("confirmed_count"),
        coalsum("failure_count"),
        coalsum("measurement_count"),
    ]
    table = sql.table("counters")
    where = []
    query_params = {}

    if domain:
        # Join in domain_input table and filter by domain
        table = table.join(
            sql.table("domain_input"), sql.text("counters.input = domain_input.input"),
        )
        where.append(sql.text("domain = :domain"))
        query_params["domain"] = domain

    if category_code:
        # Join in citizenlab table and filter by category_code
        table = table.join(
            sql.table("citizenlab"), sql.text("citizenlab.url = counters.input"),
        )
        where.append(sql.text("category_code = :category_code"))
        query_params["category_code"] = category_code

    if probe_cc:
        where.append(sql.text("probe_cc = :probe_cc"))
        query_params["probe_cc"] = probe_cc

    if probe_asn is not None:
        if probe_asn.startswith("AS"):
            probe_asn = probe_asn[2:]
        probe_asn = int(probe_asn)
        where.append(sql.text("probe_asn = :probe_asn"))
        query_params["probe_asn"] = probe_asn

    if since:
        since = parse_date(since)
        where.append(sql.text("measurement_start_day > :since"))
        query_params["since"] = since

    if until:
        until = parse_date(until)
        where.append(sql.text("measurement_start_day <= :until"))
        query_params["until"] = until

    if axis_x:
        # TODO: check if the value is a valid colum name
        cols.append(column(axis_x))
        if axis_x == "category_code":
            # Join in citizenlab table
            table = table.join(
                sql.table("citizenlab"), sql.text("citizenlab.url = counters.input"),
            )

    if axis_y:
        # TODO: check if the value is a valid colum name
        cols.append(column(axis_y))
        if axis_y == "category_code":
            # Join in citizenlab table
            table = table.join(
                sql.table("citizenlab"), sql.text("citizenlab.url = counters.input"),
            )

    # Assemble query
    where_expr = and_(*where)
    query = select(cols).where(where_expr).select_from(table)

    # Add group-by
    if axis_x:
        query = query.group_by(column(axis_x)).order_by(column(axis_x))

    if axis_y:
        query = query.group_by(column(axis_y)).order_by(column(axis_y))

    try:
        q = current_app.db_session.execute(query, query_params)

        if dimension_cnt == 2:
            r = [dict(row) for row in q]

        elif axis_x or axis_y:
            r = [dict(row) for row in q]

        else:
            r = dict(q.fetchone())

        if format == "CSV":
            return _convert_to_csv(r)

        response = jsonify({"v": 0, "dimension_count": dimension_cnt, "result": r})
        if cacheable:
            response.cache_control.max_age = 3600 * 24
        return response

    except Exception as e:
        return jsonify({"v": 0, "error": str(e)})
