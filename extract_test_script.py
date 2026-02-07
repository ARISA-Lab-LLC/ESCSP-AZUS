#extract_test_script.py


from azus import *
from escsp_azus import *
import os 
import socket

#Return the local computer / host name.
hostname = socket.gethostname()

if hostname.lower() == 'Galileo.local'.lower() :
    base_path = "/Volumes/DB_Library/Dropbox/programs/ESCSP_Data/AZUS_Workspace"
    resources_dir = os.path.join(base_path,"Resources") 
    



output_dir = os.path.join(base_path, "Staging_Area")

input_csv_path=os.path.join(resources_dir,"2024_Total_Zenodo_Form_Spreadsheet.csv") 

output_csv_path=os.path.join(output_dir,"total_eclipse_data.csv") 
esid_value="004"
error_log_path=os.path.join(output_dir,"csv_extract_error.txt")

#Section to rename CSV headers based on a mapping dictionary defined by the README template fields to CSV header fields.
my_mapping = {
    'Eclipse Percent (%)': 'coverage',
    'Eclipse Date':'date',
    'Local Eclipse Type':'eclipse_label',
    'ESID':'esid',
    'Latitude':'latitude',
    'Longitude':'longitude',
    'Data Collector Start Time Notes':'start_time_notes',
    'WAV Files Time & Date Settings':'time_date_mode',
    'Eclipse Start Time (UTC) (1st Contact)':'first_contact',
    'Totality Start Time (UTC) (2nd Contact)':'second_contact',
    'Eclipse Maximum (UTC)':'maximum_time',
    'Totality End Time (UTC) (3rd Contact)':'third_contact',
    'Eclipse End Time (UTC) (4th Contact)':'fourth_contact'
    }
    

extract_by_esid( input_csv_path, output_csv_path,
    esid_value,error_log_path=error_log_path
    )


rows, orig, new, unmapped = rename_csv_headers(
    input_csv=output_csv_path,
    output_csv=os.path.join(output_dir,"total_eclipse_data_4readme.csv"),
    header_mapping=my_mapping
    )

