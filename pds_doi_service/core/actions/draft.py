#
#  Copyright 2020, by the California Institute of Technology.  ALL RIGHTS
#  RESERVED. United States Government Sponsorship acknowledged. Any commercial
#  use must be negotiated with the Office of Technology Transfer at the
#  California Institute of Technology.
#

"""
========
draft.py
========

Contains the definition for the Draft action of the Core PDS DOI Service.
"""

import copy
import os
import requests
from lxml import etree
from os.path import exists, join

from pds_doi_service.core.actions.action import DOICoreAction
from pds_doi_service.core.actions.list import DOICoreActionList
from pds_doi_service.core.entities.doi import DoiStatus
from pds_doi_service.core.input.exceptions import (UnknownNodeException,
                                                   DuplicatedTitleDOIException,
                                                   UnexpectedDOIActionException,
                                                   NoTransactionHistoryForLIDVIDException,
                                                   TitleDoesNotMatchProductTypeException,
                                                   InputFormatException,
                                                   WarningDOIException,
                                                   CriticalDOIException)
from pds_doi_service.core.input.node_util import NodeUtil
from pds_doi_service.core.input.osti_input_validator import OSTIInputValidator
from pds_doi_service.core.input.pds4_util import DOIPDS4LabelUtil
from pds_doi_service.core.outputs.osti import DOIOutputOsti
from pds_doi_service.core.outputs.osti_web_parser import DOIOstiWebParser
from pds_doi_service.core.util.doi_validator import DOIValidator
from pds_doi_service.core.util.general_util import get_logger

logger = get_logger('pds_doi_service.core.actions.draft')


class DOICoreActionDraft(DOICoreAction):
    _name = 'draft'
    _description = 'Prepare an OSTI record from PDS4 labels'
    _order = 10
    _run_arguments = ('input', 'node', 'submitter', 'lidvid', 'force', 'keyword')

    def __init__(self, db_name=None):
        super().__init__(db_name=db_name)
        self._doi_validator = DOIValidator(db_name=db_name)
        self._list_obj = DOICoreActionList(db_name=db_name)

        self._input = None
        self._node = None
        self._submitter = None
        self._lidvid = None
        self._force = False
        self._target = None
        self._keyword = None

    @classmethod
    def add_to_subparser(cls, subparsers):
        action_parser = subparsers.add_parser(
            cls._name, description='Create a draft of OSTI records, '
                                   'from a single or list of PDS4 labels'
        )

        node_values = NodeUtil.get_permissible_values()
        action_parser.add_argument(
            '-i', '--input', required=False,
            metavar='input/bundle_in_with_contributors.xml',
            help='An input PDS4 label. May be a local path or an HTTP address '
                 'resolving to a label file. Multiple inputs may be provided '
                 'via comma-delimited list. Must be provided if --lidvid is not '
                 'specified.'
        )
        action_parser.add_argument(
            '-n', '--node', required=True,  metavar='"img"',
            help='The PDS Discipline Node in charge of the DOI. Authorized '
                 'values are: ' + ','.join(node_values)
        )
        action_parser.add_argument(
            '-s', '--submitter', required=True, metavar='"my.email@node.gov"',
            help='The email address to associate with the Draft record.'
        )
        action_parser.add_argument(
            '-l', '--lidvid', required=False,
            metavar='urn:nasa:pds:lab_shocked_feldspars::1.0',
            help='A LIDVID for an existing DOI record to move back to draft '
                 'status. Must be provided if --input is not specified.'
        )
        action_parser.add_argument(
            '-f', '--force', required=False, action='store_true',
            help='If provided, forces the action to proceed even if warnings are '
                 'encountered during submission of the draft record to the '
                 'database. Without this flag, any warnings encountered are '
                 'treated as fatal exceptions.',
        )
        action_parser.add_argument(
            '-k', '--keyword', required=False, metavar='"Image"',
            help='Extra keywords to associate with the Draft record. Multiple '
                 'keywords must be separated by ",". Ignored when used with the '
                 '--lidvid option.'
        )
        action_parser.add_argument(
            '-t', '--target',  required=False, default='osti', metavar='osti',
            help='The system target to mint the DOI. Currently, only the value '
                 '"osti" is supported.'
        )

    def _set_lidvid_to_draft(self, lidvid):
        """
        Sets the status of the transaction record corresponding to the provided
        LIDVID back draft. This can be typical for records that do not advance
        past the review step.

        Parameters
        ----------
        lidvid : str
            The LIDVID associated to the record to set to draft.

        Returns
        -------
        doi_label : str
            The OSTI XML label for the provided LIDVID reflecting its draft
            (pending) status.

        Raises
        ------
        NoTransactionHistoryForLIDVIDException
            If an entry for the provided LIDVID exists in the transaction
            database, but no local transaction history can be found.

        """
        # Get the output OSTI label produced from the last transaction
        # with this LIDVID
        transaction_record = self._list_obj.transaction_for_lidvid(lidvid)

        # Make sure we can locate the output OSTI label associated with this
        # transaction
        transaction_location = transaction_record['transaction_key']
        osti_label_file = join(transaction_location, 'output.xml')

        if not exists(osti_label_file):
            raise NoTransactionHistoryForLIDVIDException(
                f'Could not find an OSTI Label associated with LIDVID {lidvid}. '
                'The database and transaction history location may be out of sync. '
                'Please try resubmitting the record in reserve or draft.'
            )

        # Label could contain entries for multiple LIDVIDs, so extract
        # just the one we care about
        lidvid_record = DOIOstiWebParser.get_record_for_lidvid(
            osti_label_file, lidvid
        )

        # Format label into an in-memory DOI object
        dois, errors = DOIOstiWebParser.response_get_parse_osti_xml(
            bytes(lidvid_record, encoding='utf-8')
        )

        doi = dois[0]

        # Update the status back to draft while noting the previous status
        doi.previous_status = doi.status
        doi.status = DoiStatus.Draft

        # Update the output label to reflect new draft status
        doi_label = DOIOutputOsti().create_osti_doi_draft_record(doi)

        # Re-commit transaction to official roll DOI back to draft status
        transaction = self.m_transaction_builder.prepare_transaction(
            self._node, self._submitter, [doi], input_path=osti_label_file,
            output_content=doi_label
        )

        # Commit the transaction to the database
        transaction.log()

        return doi_label

    def _draft_input_files(self, inputs):
        """
        Creates draft records for the list of input files/locations.

        Parameters
        ----------
        inputs : str
            Comma-delimited listing of the inputs to produce draft records for.
            These may be local paths to a file or directory, or remote URLs.

        Returns
        -------
        doi_label : str
            A OSTI XML label containing the draft records for requested inputs.

        Raises
        ------
        CriticalDOIException
            If any errors occur during creation of the draft records.

        """
        try:
            contributor_value = NodeUtil().get_node_long_name(self._node)

            # The value of input can be a list of names, or a directory.
            # Resolve that to a list of names.
            list_of_names = self._resolve_input_into_list_of_names(inputs)

            # OSTI uses 'records' as the root tag.
            o_doi_labels = etree.Element("records")

            # For each name found, transform the PDS4 label to an OSTI record,
            # then concatenate that record to o_doi_label to return.
            for input_file in list_of_names:
                doi_label = self._run_single_file(
                    input_file, self._node, self._submitter, contributor_value,
                    self._force, self._keyword
                )

                # It is possible that the value of doi_label is None if the file
                # is not a valid label.
                if not doi_label:
                    continue

                # Concatenate each label to o_doi_labels to return.
                doc = etree.fromstring(doi_label.encode())

                for element in doc.iter():
                    # OSTI uses 'record' tag for each record.
                    if element.tag == 'record':
                        # Add the 'record' element
                        o_doi_labels.append(copy.copy(element))

            # Make the output nice by indenting it.
            etree.indent(o_doi_labels)

            return etree.tostring(o_doi_labels, pretty_print=True).decode()
        except UnknownNodeException as err:
            raise CriticalDOIException(str(err))

    def _resolve_input_into_list_of_names(self, input_labels):
        """
        Receives a string of input labels which can be a single location
        or a comma-delimited list of locations. The function returns the list
        of names parsed from the input string.
        """
        o_list_of_names = []

        # Split the input using a comma, then inspect each token to check
        # if it is a local path or a URL.
        split_tokens = input_labels.split(',')

        for token in split_tokens:
            # Only save the file name if it is not an empty string as in the
            # case of a comma being the last character:
            #    -i https://pds-imaging.jpl.nasa.gov/data/nsyt/insight_cameras/data/collection_data.xml,
            # or no name provided with just a comma:
            #    -i ,
            if len(token) > 0:
                if os.path.isdir(token):
                    # Get all file names in the directory.
                    # Note that the top level directory needs to precede the
                    # file name in the for loop.
                    list_of_names_from_token = [os.path.join(token, f)
                                                for f in os.listdir(token)
                                                if os.path.isfile(os.path.join(token, f))]

                    o_list_of_names.extend(list_of_names_from_token)
                else:
                    # The token is either the name of a file or a URL.
                    # Either way, add it to the list.
                    o_list_of_names.append(token)

        return o_list_of_names

    def _add_extra_keywords(self, keywords, io_doi):
        """
        Adds any extra keywords to the already produced DOI object.
        """
        # The keywords are comma separated. The io_doi.keywords field is a set.
        tokens = keywords.split(',')

        for one_keyword in tokens:
            io_doi.keywords.add(one_keyword.strip())

        return io_doi

    def _transform_pds4_label_into_osti_record(self, input_file,
                                               contributor_value, keywords):
        """
        Receives an XML PDS4 input file and transforms it into an OSTI record.
        """
        # Set to None to signify an input file that does not end with '.xml'
        o_doi_label = None
        o_doi = None

        # parse input_file
        if not input_file.startswith('http'):
            # Only process .xml files and print WARNING for any other files,
            # then continue.
            if input_file.endswith('.xml'):
                try:
                    xml_tree = etree.parse(input_file)
                except OSError as err:
                    msg = f'Error reading file {input_file}, reason: {str(err)}'
                    logger.error(msg)
                    raise InputFormatException(msg)
            else:
                msg = f"File {input_file} was not processed, only .xml files " \
                      "are supported"
                logger.warning(msg)
                return o_doi_label, o_doi

        else:
            # A URL gets read into memory.
            response = requests.get(input_file)
            xml_tree = etree.fromstring(response.content)

        label_util = DOIPDS4LabelUtil(
            landing_page_template=self._config.get('LANDING_PAGES', 'url')
        )

        o_doi = label_util.get_doi_fields_from_pds4(xml_tree)
        o_doi.publisher = self._config.get('OTHER', 'doi_publisher')
        o_doi.contributor = contributor_value

        # Add 'status' field so the ranking in the workflow can be determined.
        o_doi.status = DoiStatus.Draft

        # Add any extra keywords provided by the user.
        if keywords:
            self._add_extra_keywords(keywords, o_doi)

        # Add the node long name (contributor) as a keyword as well.
        o_doi.keywords.add(contributor_value)

        # Generate the output OSTI record
        o_doi_label = DOIOutputOsti().create_osti_doi_draft_record(o_doi)

        # Return the label (which is text) and a dictionary 'o_doi' representing
        # all values parsed.
        return o_doi_label, o_doi

    def _run_single_file(self, input_file, node, submitter, contributor_value,
                         force_flag, keywords=None):
        logger.info(f"input_file {input_file}")
        logger.debug(f"force_flag,input_file {force_flag, input_file}")

        try:
            # Transform the PDS4 label to an OSTI record.
            doi_label, doi_obj = self._transform_pds4_label_into_osti_record(
                input_file, contributor_value, keywords
            )

            if doi_label:
                # Validate the doi_label content against schematron for correctness.
                # If the input is correct no exception is thrown and code can
                # proceed to database validation and then submission.
                OSTIInputValidator().validate(doi_label)

                if self._config.get('OTHER', 'draft_validate_against_xsd_flag').lower() == 'true':
                    self._doi_validator.validate_against_xsd(doi_label)

            if doi_obj:
                self._doi_validator.validate(doi_obj)
            else:
                return None

            # Use the service of TransactionBuilder to prepare all things
            # related to writing a transaction.
            transaction = self.m_transaction_builder.prepare_transaction(
                node, submitter, [doi_obj], input_path=input_file,
                output_content=doi_label
            )

            # Commit the transaction to the database
            transaction.log()

            return doi_label
        # Treat warnings as exceptions if force flag is not provided
        except (DuplicatedTitleDOIException, UnexpectedDOIActionException,
                TitleDoesNotMatchProductTypeException) as err:
            if not force_flag:
                # If the user did not use force_flag, re-raise the exception.
                raise WarningDOIException(str(err))
            else:
                # Just log that the warning occurred
                logger.warn(str(err))
        # Catch all other exceptions as errors
        except InputFormatException as err:
            raise CriticalDOIException(err)

    def run(self, **kwargs):
        """
        Receives a number of input label locations from which to create a
        draft Data Object Identifier (DOI). Each location may be a local directory
        or file path, or a remote HTTP address to the input XML PDS4 label file.

        Parameters
        ----------
        kwargs : dict
            Contains the arguments for the Draft action as parsed from the
            command-line.

        Raises
        ------
        ValueError
            If the provided arguments are invalid.

        """
        self.parse_arguments(kwargs)

        # Make sure we've been given something to work with
        if self._input is None and self._lidvid is None:
            raise ValueError('A value must be provided for either --input or '
                             '--lidvid when using the Draft action.')

        if self._lidvid:
            return self._set_lidvid_to_draft(self._lidvid)

        if self._input:
            return self._draft_input_files(self._input)
