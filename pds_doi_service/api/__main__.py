#!/usr/bin/env python3

import connexion
from flask_cors import CORS

from pds_doi_service.api import encoder


def main():

    app = connexion.App(__name__, specification_dir='swagger/')
    CORS(app.app)
    app.app.json_encoder = encoder.JSONEncoder
    app.add_api('swagger.yaml',
                arguments={'title': 'Planetary Data System DOI Service API'},
                pythonic_params=True)

    app.run(port=8080)


if __name__ == '__main__':
    main()
