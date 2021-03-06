#
#  Copyright 2020, by the California Institute of Technology.  ALL RIGHTS
#  RESERVED. United States Government Sponsorship acknowledged. Any commercial
#  use must be negotiated with the Office of Technology Transfer at the
#  California Institute of Technology.
#

"""
==================
dois_controller.py
==================

Contains the request handlers for the PDS DOI API.
"""

import csv
import json
from os.path import exists, join
from tempfile import NamedTemporaryFile

import connexion
from flask import current_app

from pds_doi_service.api.util import format_exceptions
from pds_doi_service.api.models import DoiRecord, DoiSummary
from pds_doi_service.core.actions.draft import DOICoreActionDraft
from pds_doi_service.core.actions.list import DOICoreActionList
from pds_doi_service.core.actions.release import DOICoreActionRelease
from pds_doi_service.core.actions.reserve import DOICoreActionReserve
from pds_doi_service.core.input.exceptions import (UnknownLIDVIDException,
                                                   NoTransactionHistoryForLIDVIDException,
                                                   WarningDOIException)
from pds_doi_service.core.input.input_util import DOIInputUtil
from pds_doi_service.core.outputs.osti_web_parser import DOIOstiWebParser


def _get_db_name():
    """
    Helper function to return a database name to use with the endpoint.
    Used primarily for substituting in a pre-existing database when testing.

    Returns
    -------
    db_name : str
        Path to the database instance to use with an action class.
        If testing mode on the Flask app is disabled, or no name was specified
        with the request arguments, then None is returned.

    """
    db_name = None

    # If were testing, check if theres a pre-defined database we should be using
    if current_app.config['TESTING']:
        db_name = connexion.request.args.get('db_name')

    return db_name


def _write_csv_from_labels(temp_file, labels):
    """
    Writes the provided list of labels in CSV format to the open temporary
    file handle. The contents are flushed to disk before this function returns.

    Parameters
    ----------
    temp_file : tempfile.NamedTemporaryFile
        The open temporary file to write CSV contents to.
    labels : list of dict
        List of labels to be written out in CSV format.

    """
    csv_writer = csv.DictWriter(
        temp_file, fieldnames=DOIInputUtil.MANDATORY_COLUMNS
    )

    csv_writer.writeheader()

    for label in labels:
        csv_writer.writerow(label)

    temp_file.flush()


def _records_from_dois(dois, node=None, submitter=None, osti_record=None):
    """
    Reformats a list of DOI objects into a corresponding list of DoiRecord
    objects.

    Parameters
    ----------
    dois : list of Doi
        The list of pds_doi_service.core.entities.doi.Doi objects to reformat
        into DoiRecords.
    node : str, optional,
        The PDS node to associate with each record.
    submitter : str, optional
        Submitter email address to associate with each record.
    osti_record : str, optional
        OSTI XML record to associate with each record.

    Returns
    -------
    records : list of DoiRecord
        The records produced from the provided Doi objects.

    """
    records = []

    for doi in dois:
        records.append(
            DoiRecord(
                doi=doi.doi, lidvid=doi.related_identifier, node=node,
                submitter=submitter, status=doi.status,
                creation_date=doi.date_record_added,
                update_date=doi.date_record_updated,
                record=osti_record,
                message=doi.message
            )
        )

    return records


def get_dois(doi=None, submitter=None, node=None, lid=None, start_date=None,
             end_date=None):
    """
    List the DOI requests within the transaction database which match
    the specified criteria. If no criteria are provided, all database entries
    are returned.

    Parameters
    ----------
    doi : list of str, optional
        List of DOIs to fetch from transaction database.
    submitter : list of str, optional
        List of submitter email addresses to filter DOIs by.
    node : list of str, optional
        List of PDS node names cited as contributor of the DOI to filter by.
        Each identifier must be one of the valid PDS steward IDs.
    lid : list of str, optional
        List of LIDs to filter DOIs by. An LID may include the VID appended to
        the end.
    start_date : str
        A start date to filter resulting DOI records by. Only records with an
        update time after this date will be returned. Value must be of the form
        <YYYY>-<mm>-<dd>T<HH>:<MM>:<SS>.<ms>
    end_date : str
        An end date to filter resulting DOI records by. Only records with an
        update time prior to this date will be returned. Value must be of the
        form <YYYY>-<mm>-<dd>T<HH>:<SS>.<ms>
    Returns
    -------
    records : list of DoiSummary
        The available DOI records from within the transaction database that
        match the requested criteria.

    """
    list_action = DOICoreActionList(db_name=_get_db_name())

    # List action expects multiple inputs as comma-delimited
    if doi:
        doi = ','.join(doi)

    if submitter:
        submitter = ','.join(submitter)

    if node:
        node = ','.join(node)

    lidvid = None

    if lid:
        # Separate the LID from LIDVID
        lidvid = list(filter(lambda s: '::' in s, lid))
        lid = list(set(lid) - set(lidvid))

        lidvid = ','.join(lidvid)
        lid = ','.join(lid)

    list_kwargs = {
        'doi': doi,
        'lid': lid,
        'lidvid': lidvid,
        'submitter': submitter,
        'node': node,
        'start_update': start_date,
        'end_update': end_date
    }

    try:
        results = list_action.run(**list_kwargs)
    except ValueError as err:
        # Most likely from an malformed start/end date. Report back "Invalid
        # argument" code
        return format_exceptions(err), 400
    except Exception as err:
        # Treat any unexpected Exception as an "Internal Error" and report back
        return format_exceptions(err), 500

    records = []

    for result in json.loads(results):
        lidvid = result['lid']

        # Check if we got back a vid to append to the lid
        if result['lid'] and result['vid']:
            lidvid = '::'.join([result['lid'], result['vid']])

        records.append(
            DoiSummary(
                doi=result['doi'], lidvid=lidvid, node=result['node_id'],
                submitter=result['submitter'], status=result['status'],
                update_date=result['update_date']
            )
        )

    return records, 200


def post_dois(action, submitter, node, url=None, body=None, force=False):
    """
    Submit a DOI in reserve or draft status. The input to the action may be
    either a JSON labels payload (for reserve or draft), or a URL to a PDS4
    XML label file (draft only).

    Parameters
    ----------
    action : str
        The submission action to perform. Must be one of "reserve" or "draft".
    submitter : str
        Email address of the submission requester.
    node : str
        The PDS node name to cite as contributor of the DOI. Must be one of the
        valid PDS steward IDs.
    url : str, optional
        URL to provide as the record to register a DOI for. URL must start with
        either "http://" or "https://" and resolve to a valid PDS4 label in XML
        format. Only used when action is set to "draft".
    body : str or dict
        requestBody contents. If provided, should contain an PSD4 label (for
        draft) or one or more LabelPayload structures (for reserve). Required if
        the action is set to "reserve", otherwise it can be used optionally in
        lieu of url when the action is set to "draft".
    force : bool
        If true, forces a reserve request to completion, ignoring any warnings
        encountered. Has no effect for draft requests.

    Returns
    -------
    record : DoiRecord
        A record of the DOI submission request.
    response_code : int
        The HTTP response code corresponding to the result.

    """
    try:
        if action == 'reserve':
            # Extract the list of labels from the requestBody, if one was provided
            if not connexion.request.is_json:
                raise ValueError('No JSON requestBody provided for reserve POST '
                                 'request.')
            else:
                body = connexion.request.get_json()

            reserve_action = DOICoreActionReserve(db_name=_get_db_name())

            with NamedTemporaryFile('w', prefix='labels_', suffix='.csv') as csv_file:
                _write_csv_from_labels(csv_file, body['labels'])

                reserve_kwargs = {
                    'node': node,
                    'submitter': submitter,
                    'input': csv_file.name,
                    'force': force,
                    'dry_run': False
                }

                osti_label = reserve_action.run(**reserve_kwargs)
        elif action == 'draft':
            if not body and not url:
                raise ValueError('No requestBody or URL parameter provided '
                                 'as input to draft request. One or the other '
                                 'must be provided.')

            draft_action = DOICoreActionDraft(db_name=_get_db_name())

            # Determine how the input label(s) was sent
            if body:
                with NamedTemporaryFile('wb', prefix='labels_', suffix='.xml') as xml_file:
                    xml_file.write(body)
                    xml_file.flush()

                    draft_kwargs = {
                        'node': node,
                        'submitter': submitter,
                        'input': xml_file.name
                    }

                    osti_label = draft_action.run(**draft_kwargs)
            else:
                draft_kwargs = {
                    'node': node,
                    'submitter': submitter,
                    'input': url
                }

                osti_label = draft_action.run(**draft_kwargs)
        else:
            raise ValueError('Action must be either "draft" or "reserve". '
                             'Received "{}"'.format(action))
    # These exceptions indicate some kind of input error, so return the
    # Invalid Argument code
    except (WarningDOIException, ValueError) as err:
        return format_exceptions(err), 400
    # For everything else, return the Internal Error code
    except Exception as err:
        return format_exceptions(err), 500

    # Parse the OSTI XML string back into a list of DOIs
    dois, _ = DOIOstiWebParser().response_get_parse_osti_xml(
        bytes(osti_label, encoding='utf-8')
    )

    records = _records_from_dois(
        dois, node=node, submitter=submitter, osti_record=osti_label
    )

    return records, 200


def post_submit_doi(lidvid, force=None):
    """
    Move a DOI record from draft/reserve status to "review".

    Parameters
    ----------
    lidvid : str
        The LIDVID associated with the record to submit for review.
    force : bool, optional
        If true, forces a submit request to completion, ignoring any warnings
        encountered.

    Returns
    -------
    record : DoiRecord
        Record of the DOI submit action.

    """
    # A submit action is the same as invoking the release endpoint with
    # --no-review set to False
    kwargs = {'lidvid': lidvid, 'force': force, 'no_review': False}

    return post_release_doi(**kwargs)


def post_release_doi(lidvid, force=False, **kwargs):
    """
    Move a DOI record from draft/reserve status to "release".

    Parameters
    ----------
    lidvid : str
        The LIDVID associated with the record to release.
    force : bool, optional
        If true, forces a release request to completion, ignoring any warnings
        encountered.
    kwargs : dict
        Additional keyword arguments to forward to the DOI release action.

    Returns
    -------
    record : DoiRecord
        Record of the DOI release action.

    """
    try:
        list_action = DOICoreActionList(db_name=_get_db_name())

        # Get the latest transaction record for this LIDVID
        list_record = list_action.transaction_for_lidvid(lidvid)

        # Make sure we can locate the output OSTI label associated with this
        # transaction
        transaction_location = list_record['transaction_key']
        osti_label_file = join(transaction_location, 'output.xml')

        if not exists(osti_label_file):
            raise NoTransactionHistoryForLIDVIDException(
                'Could not find an OSTI Label associated with LIDVID {}. '
                'The database and transaction history location may be out of sync. '
                'Please try resubmitting the record in reserve or draft.'
                .format(lidvid)
            )

        # An output OSTI label may contain entries other than the requested
        # LIDVID, extract only the appropriate record into its own temporary
        # XML file and feed it to the release action
        with NamedTemporaryFile('w', prefix='output_', suffix='.xml') as xml_file:
            xml_file.write(DOIOstiWebParser.get_record_for_lidvid(osti_label_file, lidvid))
            xml_file.flush()

            # Prepare the release action
            release_action = DOICoreActionRelease(db_name=_get_db_name())

            release_kwargs = {
                'node': list_record['node_id'],
                'submitter': list_record['submitter'],
                'input': xml_file.name,
                'force': force,
                # Default for this endpoint should be to skip review and release
                # directly to OSTI
                'no_review': kwargs.get('no_review', True)
            }

            osti_release_label = release_action.run(**release_kwargs)

        dois, errors = DOIOstiWebParser().response_get_parse_osti_xml(
            bytes(osti_release_label, encoding='utf-8')
        )

        # Propagate any errors returned from OSTI in a single exception
        if errors:
            raise WarningDOIException(
                'Received the following errors from the release request to OSTI:\n'
                '{}'.format('\n'.join(errors))
            )
    except (ValueError, WarningDOIException) as err:
        # Some warning or error prevented release of the DOI
        return format_exceptions(err), 400
    except UnknownLIDVIDException as err:
        # Could not find an entry for the requested LIDVID
        return format_exceptions(err), 404
    except Exception as err:
        # Treat any unexpected Exception as an "Internal Error" and report back
        return format_exceptions(err), 500

    records = _records_from_dois(
        dois, node=list_record['node_id'], submitter=list_record['submitter'],
        osti_record=osti_release_label
    )

    return records, 200


def get_doi_from_id(lidvid):  # noqa: E501
    """
    Get the status of a DOI from the transaction database.

    Parameters
    ----------
    lidvid : str
        The LIDVID associated with the record to status.

    Returns
    -------
    record : DoiRecord
        The record for the requested LIDVID.

    """
    list_action = DOICoreActionList(db_name=_get_db_name())

    # Check for full lidvid vs. just a lid and map the list action arg accordingly
    list_kwargs = {
        'lidvid' if '::' in lidvid else 'lid': lidvid
    }

    try:
        list_results = json.loads(list_action.run(**list_kwargs))

        if not list_results:
            raise UnknownLIDVIDException(
                'No record(s) could be found for LIDVID {}'.format(lidvid)
            )

        # Extract the latest record from all those returned
        list_record = next(filter(lambda list_result: list_result['is_latest'],
                                  list_results))

        # Make sure we can locate the output OSTI label associated with this
        # transaction
        transaction_location = list_record['transaction_key']
        osti_label_file = join(transaction_location, 'output.xml')

        if not exists(osti_label_file):
            raise NoTransactionHistoryForLIDVIDException(
                'Could not find an OSTI Label associated with LIDVID {}. '
                'The database and transaction history location may be out of sync. '
                'Please try resubmitting the record in reserve or draft.'
                .format(lidvid)
            )

        # Get only the record corresponding to the requested LIDVID
        osti_label_for_lidvid = DOIOstiWebParser.get_record_for_lidvid(osti_label_file, lidvid)
    except UnknownLIDVIDException as err:
        # Return "not found" code
        return format_exceptions(err), 404
    except Exception as err:
        # Treat any unexpected Exception as an "Internal Error" and report back
        return format_exceptions(err), 500

    # Parse the label associated with the lidvid so we can return a full DoiRecord
    dois, _ = DOIOstiWebParser().response_get_parse_osti_xml(
        bytes(osti_label_for_lidvid, encoding='utf-8')
    )

    records = _records_from_dois(
        dois, node=list_record['node_id'], submitter=list_record['submitter'],
        osti_record=osti_label_for_lidvid
    )

    # Should only ever be one record since we filtered by lidvid
    return records[0], 200


def put_doi_from_id(lidvid, submitter=None, node=None, url=None):  # noqa: E501
    """
    Update the record associated with an existing DOI.

    Notes
    -----
    This endpoint has deprecated in favor of the GET /dois endpoint.

    Parameters
    ----------
    lidvid : str
        The LIDVID associated with the record to update.
    submitter : str, optional
        Email address of the DOI update requester.
    node : str, optional
        The PDS node name to cite as contributor of the DOI. Must be one of the
        valid PDS steward IDs.
    url : str, optional
        URL to provide as the record to update the DOI with. URL must start with
        either "http://" or "https://" and resolve to a valid PDS4 label in XML
        format.

    Returns
    -------
    record : DoiRecord
        A record of the DOI update transaction.

    """
    return format_exceptions(
        NotImplementedError('Please use the POST /dois/{lidvid} endpoint for record '
                            'update')
    ), 501
