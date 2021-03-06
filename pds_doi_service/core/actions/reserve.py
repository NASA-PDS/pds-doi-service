#
#  Copyright 2020, by the California Institute of Technology.  ALL RIGHTS
#  RESERVED. United States Government Sponsorship acknowledged. Any commercial
#  use must be negotiated with the Office of Technology Transfer at the
#  California Institute of Technology.
#

"""
==========
reserve.py
==========

Contains the definition for the Reserve action of the Core PDS DOI Service.
"""

from datetime import datetime
import os
import requests

from lxml import etree

from pds_doi_service.core.actions.action import DOICoreAction
from pds_doi_service.core.entities.doi import DoiStatus
from pds_doi_service.core.input.exceptions import (CriticalDOIException,
                                                   DuplicatedTitleDOIException,
                                                   InputFormatException,
                                                   TitleDoesNotMatchProductTypeException,
                                                   UnexpectedDOIActionException,
                                                   UnknownNodeException,
                                                   collect_exception_classes_and_messages,
                                                   raise_warn_exceptions)
from pds_doi_service.core.input.input_util import DOIInputUtil
from pds_doi_service.core.input.node_util import NodeUtil
from pds_doi_service.core.input.osti_input_validator import OSTIInputValidator
from pds_doi_service.core.input.pds4_util import DOIPDS4LabelUtil
from pds_doi_service.core.outputs.osti import DOIOutputOsti
from pds_doi_service.core.outputs.osti_web_client import DOIOstiWebClient
from pds_doi_service.core.util.doi_validator import DOIValidator
from pds_doi_service.core.util.general_util import get_logger

logger = get_logger('pds_doi_core.actions.reserve')


class DOICoreActionReserve(DOICoreAction):
    _name = 'reserve'
    _description = 'Create or update a DOI before the data is published'
    _order = 0
    _run_arguments = ('input', 'node', 'submitter', 'dry_run', 'force')

    def __init__(self, db_name=None):
        super().__init__(db_name=db_name)
        self._doi_validator = DOIValidator(db_name=db_name)
        self._input = None
        self._node = None
        self._submitter = None
        self._force = False
        self._dry_run = True

    @classmethod
    def add_to_subparser(cls, subparsers):
        action_parser = subparsers.add_parser(
            cls._name, description='Create a DOI for one or more unpublished datasets. '
                                   'The input is a spreadsheet or CSV file '
                                   'containing records to reserve DOIs for.')

        node_values = NodeUtil.get_permissible_values()
        action_parser.add_argument(
            '-n', '--node', required=True, metavar='"img"',
            help="The PDS Discipline Node in charge of the submission of the DOI. "
                 "Authorized values are: " + ','.join(node_values)
        )
        action_parser.add_argument(
            '-f', '--force', required=False, action='store_true',
            help='If provided, forces the reserve action to proceed even if '
                 'warnings are encountered during submission of the reserve to '
                 'OSTI. Without this flag, any warnings encountered are '
                 'treated as fatal exceptions.'
        )
        action_parser.add_argument(
            '-i', '--input', required=True,
            metavar='input/DOI_Reserved_GEO_200318.csv',
            help='A PDS4 XML label, or XLS/CSV spreadsheet file with the '
                 'following columns: ' + ','.join(DOIInputUtil.MANDATORY_COLUMNS)
        )
        action_parser.add_argument(
            '-s', '--submitter-email', required=True,
            metavar='"my.email@node.gov"',
            help='The email address to associate with the Reserve request.'
        )
        action_parser.add_argument(
            '-d', '--dry-run', required=False, action='store_true',
            help="Performs the Reserve request without submitting the record to "
                 "OSTI. The record is logged to the local database with a status "
                 "of 'reserved_not_submitted'."
        )

    def _read_from_path(self, path):
        if os.path.isfile(path):
            if path.endswith('.xml'):
                return self._read_from_local_pdf4(path)
            elif path.endswith('.xlsx') or path.endswith('.xls'):
                return self._read_from_local_xlsx(path)
            elif path.endswith('.csv'):
                return self._read_from_local_csv(path)
            else:
                logger.info(f'file {path} not supported')
        else:
            dois = []

            for sub_path in os.listdir(path):
                dois.extend(self._read_from_path(os.path.join(path, sub_path)))

            return dois

    def _read_from_remote_pds4(self, url):
        try:
            response = requests.get(url)
            xml_tree = etree.fromstring(response.content)
            label_util = DOIPDS4LabelUtil(
                landing_page_template=self._config.get('LANDING_PAGES', 'url')
            )
            doi = label_util.get_doi_fields_from_pds4(xml_tree)

            return [doi]
        except OSError as err:
            msg = f'Error reading file {url}, reason: {str(err)}'
            logger.error(msg)
            raise InputFormatException(msg)

    def _read_from_local_pdf4(self, path):
        # parse input
        try:
            xml_tree = etree.parse(path)
            label_util =  DOIPDS4LabelUtil(
                landing_page_template=self._config.get('LANDING_PAGES', 'url')
            )
            doi = label_util.get_doi_fields_from_pds4(xml_tree)

            return [doi]
        except OSError as err:
            msg = f'Error reading file {path}, reason: {str(err)}'
            logger.error(msg)
            raise InputFormatException(msg)

    def _read_from_local_xlsx(self, path):
        """Processes a Reserve action based on a file with an .xlsx ending."""
        try:
            dois = DOIInputUtil().parse_sxls_file(path)

            # Do a sanity check on content of dict_condition_data.
            if len(dois) == 0:
                raise InputFormatException(
                    "Length of dict_condition_data['dois'] is zero, target_url " + path
                )

            return dois
        except InputFormatException as err:
            logger.error(err)
            exit(1)
        except OSError as err:
            msg = f'Error reading file {path}, reason: {str(err)}'
            logger.error(msg)
            raise InputFormatException(msg)

    def _read_from_local_csv(self, path):
        """Processes a Reserve action based on a file with a .csv ending."""
        try:
            dois = DOIInputUtil().parse_csv_file(path)

            # Do a sanity check on content of dict_condition_data.
            if len(dois) == 0:
                raise InputFormatException(
                    "Length of dict_condition_data['dois'] is zero, target_url " + path
                )

            return dois
        except InputFormatException as err:
            logger.error(err)
            exit(1)
        except OSError as err:
            msg = f'Error reading file {path}, reason: {str(err)}'
            logger.error(msg)
            raise InputFormatException(msg)

    def _parse_input(self, input_file):
        # Check for existence first to return the message the 'behave' testing
        # expects.
        if input_file.startswith('http'):
            return self._read_from_remote_pds4(input_file)
        elif os.path.exists(input_file):
            return self._read_from_path(input_file)
        else:
            raise InputFormatException(f"Error reading file {input_file}")

    def complete_and_validate_dois(self, dois, contributor, publisher, dry_run):
        exception_classes = []
        exception_messages = []

        # Note that it is important to fill in the doi.status for all dois in
        # case an exception occurs in the validate() function.
        # If an exception occurs, the value of dois now has the correct
        # contributor, publisher and status fields filled in.
        for doi in dois:
            # First set contributor, publisher and status to the beginning of
            # the function to ensure that they are set in case of an exception.
            doi.contributor = contributor
            doi.publisher = publisher

            # Note that the mustache file must have the double quotes around the
            # status value: <record status="{{status}}">, as it is an attribute
            # of a field.

            # Add 'status' field so the ranking in the workflow can be determined
            doi.status = DoiStatus.Reserved_not_submitted if dry_run else DoiStatus.Reserved

            # Add field 'date_record_added' because the XSD requires it.
            doi.date_record_added = datetime.now().strftime('%Y-%m-%d')

        for doi in dois:
            try:
                if dry_run:
                    self._doi_validator.validate(doi)
                else:
                    self._doi_validator.validate_osti_submission(doi)
            # Collect all warnings and exceptions so they can be combined into
            # a single WarningDOIException
            except (DuplicatedTitleDOIException, UnexpectedDOIActionException,
                    TitleDoesNotMatchProductTypeException) as err:
                (exception_classes,
                 exception_messages) = collect_exception_classes_and_messages(
                    err, exception_classes, exception_messages
                )

        # If there is at least one exception caught, raise a WarningDOIException
        # with all the messages, provided the force flag is not set
        if len(exception_classes) > 0 and not self._force:
            raise_warn_exceptions(exception_classes, exception_messages)

        return dois

    def _validate_against_schematron_as_batch(self, dois, dry_run):
        # Because the function schematron validator only works on one record,
        # each must be extracted and validated one at a time.
        for doi in dois:
            # Add 'status' field so the ranking in the workflow can be determined
            doi.status = DoiStatus.Reserved_not_submitted if dry_run else DoiStatus.Reserved

            # Add field 'date_record_added' because the XSD requires it.
            doi.date_record_added = datetime.now().strftime('%Y-%m-%d')

            # The function create_osti_doi_reserved_record works off a list so
            # put doi in a list of 1: [doi]
            single_doi_label = DOIOutputOsti().create_osti_doi_reserved_record([doi])
            logger.debug(f'produced osti label is {single_doi_label}')

            # Validate the doi_label content against schematron for correctness.
            # If the input is correct no exception is thrown and code can
            # proceed to database validation and then submission.
            OSTIInputValidator().validate(single_doi_label)

    def _validate_against_xsd_as_batch(self, dois, dry_run):
        # Because the function XSD validator only works on one record, each must
        # be extracted and validated one at a time.
        for doi in dois:
            # Add 'status' field so the ranking in the workflow can be determined
            doi.status = DoiStatus.Reserved_not_submitted if dry_run else DoiStatus.Reserved

            # Add field 'date_record_added' because the XSD requires it.
            doi.date_record_added = datetime.now().strftime('%Y-%m-%d')

            # The function create_osti_doi_reserved_record works off a list so
            # put doi in a list of 1: [doi]
            single_doi_label = DOIOutputOsti().create_osti_doi_reserved_record([doi])
            logger.debug(f"single_doi_label {single_doi_label}")

            # Validate the single_doi_label against the XSD.
            self._doi_validator.validate_against_xsd(single_doi_label)

    def run(self, **kwargs):
        logger.info('run reserve')

        self.parse_arguments(kwargs)

        try:
            dois = self._parse_input(self._input)

            if self._config.get('OTHER', 'reserve_validate_against_xsd_flag').lower() == 'true':
                self._validate_against_xsd_as_batch(dois, self._dry_run)

            self._validate_against_schematron_as_batch(dois, self._dry_run)

            dois = self.complete_and_validate_dois(
                dois, NodeUtil().get_node_long_name(self._node),
                self._config.get('OTHER', 'doi_publisher'), self._dry_run
            )
            o_doi_label = DOIOutputOsti().create_osti_doi_reserved_record(dois)

            if not self._dry_run:
                dois, o_doi_label = DOIOstiWebClient().webclient_submit_existing_content(
                    o_doi_label,
                    i_url=self._config.get('OSTI', 'url'),
                    i_username=self._config.get('OSTI', 'user'),
                    i_password=self._config.get('OSTI', 'password')
                )

            transaction = self.m_transaction_builder.prepare_transaction(
                self._node, self._submitter, dois, input_path=self._input,
                output_content=o_doi_label
            )

            # Commit the transaction to the local database
            transaction.log()

            logger.debug(f"reserve_response {o_doi_label}")
            logger.debug(f"_input,self,_dry_run {self._input, self._dry_run}")
            return o_doi_label
        # Convert other errors into a CriticalDOIException to report back
        except (UnknownNodeException, InputFormatException) as err:
            raise CriticalDOIException(err)
