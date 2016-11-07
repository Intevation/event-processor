"""Access to the event/notification database."""

import string
import json
import logging

import psycopg2
import psycopg2.errorcodes


log = logging.getLogger(__name__)


def open_db_connection(config, connection_factory=None):
    params = config['database']['event']
    return psycopg2.connect(database=params['name'],
                            user=params['username'],
                            password=params['password'],
                            host=params['host'],
                            port=params['port'],
                            # sslmode=params['sslmode'],
                            connection_factory=connection_factory)


def json_array_to_mapping(jsonarray):
    """Return a json(-string) representation of a list of pairs into a mapping.
    The parameter may either be a list of key/value pairs or a JSON
    string encoding a list of pairs. This function is used for the value
    describing which notifications belong to which events returned from
    the database query
    """
    if isinstance(jsonarray, str):
        jsonarray = json.loads(jsonarray)
    mapping = {}
    for key, value in jsonarray:
        mapping.setdefault(key, []).append(value)
    return mapping


def get_pending_notifications(cur):
    """Retrieve all pending notifications from the database.
    Notifications are pending if they haven't been sent yet.
    Notifications are grouped by recipient, template, format,
    classification type and feed name so that the information about the
    events for which the notifications are sent can be aggregated.

    Also, the feed names 'Botnet-Drone-Hadoop', 'Sinkhole-HTTP-Drone'
    and 'Microsoft-Sinkhole' are replaced by 'generic_malware' before
    grouping so that event from those feeds are aggregated.

    :returns: list of aggreated notifications
    :rtype: list
    """
    # The query uses json_agg for the idmap result column instead of the
    # more obvious array_agg because the latter cannot create
    # multidimensional arrays. Arrays of ROWs might be an alternative
    # but psycopg does not support that out of the box (the array is
    # returned as a string).
    operation_str = """\
        SELECT n.email as email, n.template as template, n.format as format,
               n.classification_type as classification_type,
               n.feed_name AS feed_name,
               json_agg(ARRAY[n.events_id, n.id]) as idmap
          FROM (SELECT id, events_id, email, template, format,
                       classification_type, notification_interval,
                       CASE WHEN feed_name IN ('Botnet-Drone-Hadoop',
                                               'Sinkhole-HTTP-Drone',
                                               'Microsoft-Sinkhole')
                            THEN 'generic_malware'
                            ELSE feed_name
                       END AS feed_name
                  FROM notifications
                 WHERE intelmq_ticket IS NULL
                FOR UPDATE NOWAIT) n
      GROUP BY n.email, n.template, n.format, n.classification_type, n.feed_name
        HAVING coalesce((SELECT max(sent_at) FROM notifications n2
                         WHERE n2.email = n.email
                           AND n2.template = n.template
                           AND n2.format = n.format
                           AND n2.classification_type = n.classification_type
                           AND n2.feed_name = n.feed_name)
                        + max(n.notification_interval)
                        < CURRENT_TIMESTAMP,
                        TRUE);"""
    try:
        cur.execute(operation_str)
    except psycopg2.OperationalError as e:
        if e.pgcode == psycopg2.errorcodes.LOCK_NOT_AVAILABLE:
            log.info("Could not get db lock for pending notifications. "
                     "Probably another instance of myself is running.")
            return None
        else:
            raise

    rows = []
    for row in cur.fetchall():
        row["idmap"] = json_array_to_mapping(row["idmap"])
        rows.append(row)
    return rows


# characters allowed in identifiers in escape_sql_identifier. There are
# just the characters that are used in IntelMQ for identifiers in the
# events table.
sql_identifier_charset = set(string.ascii_letters + string.digits + "_.")


def escape_sql_identifier(ident):
    if set(ident) - sql_identifier_charset:
        raise ValueError("Event column identifier %r contains invalid"
                         " characters (%r)"
                         % (ident, set(ident) - sql_identifier_charset))
    return '"' + ident + '"'


def load_events(cur, event_ids, columns=None):
    """Return events for the ids with all or a subset of available columns.

    Use the columns parameter to specify which columns to return.

    :param cur: database connection
    :param event_ids: list of events ids
    :param columns: list of column names, defaults to all if 'None' is given.
    returns: corresponding events as a list of dictionaries
    """
    if columns is not None:
        sql_columns = ", ".join(escape_sql_identifier(col) for col in columns)
    else:
        sql_columns = "*"
    cur.execute("SELECT {} FROM events WHERE id = ANY (%s)".format(sql_columns),
                (event_ids,))

    return cur.fetchall()


def new_ticket_number(cur):
    """Draw a new unique ticket number.

    Check the database and reset the ticket counter if
    our day is past the last initialisation day.
    Raise RuntimeError if last initialisation is in the future, because
    we may potentially reuse ticket numbers if we get to this day.

    :returns: a unique ticket-number string in format YYYYMMDD-XXXXXXXX
    :rtype: string
    """
    sqlQuery = """SELECT to_char(now(), 'YYYYMMDD') AS date,
                         (SELECT to_char(initialized_for_day, 'YYYYMMDD')
                              FROM ticket_day) AS init_date,
                         nextval('intelmq_ticket_seq');"""
    cur.execute(sqlQuery)
    result = cur.fetchall()
    #log.debug(result)

    date_str = result[0]["date"]
    if date_str != result[0]["init_date"]:
        if date_str < result[0]["init_date"]:
            raise RuntimeError(
                    "initialized_for_day='{}' is in the future from now(). "
                    "Stopping to avoid reusing "
                    "ticket numbers".format(result[0]["init_date"]))

        log.debug("We have a new day, reseting the ticket generator.")
        cur.execute("ALTER SEQUENCE intelmq_ticket_seq RESTART;")
        cur.execute("UPDATE ticket_day SET initialized_for_day=%s;",
                    (date_str,));

        cur.execute(sqlQuery)
        result = cur.fetchall()
        log.debug(result)

    # create from integer: fill with 0s and cut out 8 chars from the right
    num_str = "{:08d}".format(result[0]["nextval"])[-8:]
    ticket = "{:s}-{:s}".format(date_str, num_str)
    log.debug('New ticket number "{}".'.format(ticket,))

    return ticket


def mark_as_sent(cur, notification_ids, ticket):
    "Mark notifactions with given ids as sent and set the ticket number."
    log.debug("Marking notifications_ids {} as sent.".format(notification_ids))
    cur.execute(""" UPDATE notifications
                    SET sent_at = now(),
                        intelmq_ticket = %s
                  WHERE id = ANY (%s);""",
                (ticket, notification_ids,))