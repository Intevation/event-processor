from intelmqmail.tableformat import build_table_format
import json

def create_notifications(context):
    # Read the substitutions from a file
    js = {}
    with open('/etc/intelmq/mailgen/formats/variables.json', 'r') as j:
        js = json.load(j)

    substitution_variables = js.get("substitutions")
    if substitution_variables:
        substitution_variables["ticket_prefix"] = js.get("common_strings").get("ticket_prefix")

    ## Determine the kind of Aggregation.
    aggregation = dict(context.directive["aggregate_identifier"])
    asn_or_cidr = ""  # Can also be a CC
    if "source.asn" in aggregation:
        asn_or_cidr += "about AS %s" % aggregation["source.asn"]
    elif "cidr" in aggregation:
        asn_or_cidr = "about CIDR %s" % aggregation["cidr"]
    elif "source.geolocation.cc" in aggregation:
        asn_or_cidr = "about your Country %s" % aggregation["source.geolocation.cc"]

    substitution_variables["asn_or_cidr"] = asn_or_cidr

    if context.directive["event_data_format"] == "csv":
        # Prepare Message in CSV-Format
        formats = (
            ("source.asn", "ASN"),
            ("source.ip", "IP"),
            ("time.source", "Time"),
            ("classification.identifier", "Identifier"),
            ("malware.name", "Malware"),
            ("source.port", "Port"),
            ("destination.ip", "Destination-IP"),
            ("destination.port", "Destination-Port"),
            ("destination.fqdn", "Destination-FQDN"))

        format = build_table_format("avalanche", formats)
        if context.directive["notification_format"] == "inline":
            # If Inline-Messages are wanted
            return context.mail_format_as_csv(format,substitutions=substitution_variables)
        if context.directive["notification_format"] == "attachment":
            # If Attachment-Messages are wanted
            # TODO: Call correct Method
            return context.mail_format_as_csv(format,substitutions=substitution_variables)

    return None