"""Creates deployment to accept all upload requests on Zenodo."""

from flows import accept_publish_requests

if __name__ == "__main__":
    accept_publish_requests.serve(name="accept-requests-deployment")
