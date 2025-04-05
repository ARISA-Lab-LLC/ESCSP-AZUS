"""Creates deployment for retrieving all user published records on Zenodo."""

from flows import get_published_records

if __name__ == "__main__":
    get_published_records.serve(
        name="get-published-records-deployment",
        parameters={
            # specify the directory where retrieved records should be saved
            "directory": "/Users/user/Desktop/zenodo/results/records/"
        },
    )
