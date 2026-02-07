#file_list_test_script.py


from azus import *
from escsp_azus import *
import os 
import socket

from azus import calculate_sha512


#Return the local computer / host name.
hostname = socket.gethostname()

if hostname.lower() == 'Galileo.local'.lower() :
    base_path = "/Volumes/DB_Library/Dropbox/programs/ESCSP_Data/AZUS_Workspace"
    resources_dir = os.path.join(base_path,"Resources")
    zipPath="/Volumes/DB_Library/Dropbox/programs/ESCSP_Data/AZUS_Workspace/Staging_Area/ESID_004_Staging/ESID_004_files2zip" 
    

output_dir = os.path.join(base_path, "Staging_Area")
output_csv_path = os.path.join(zipPath, "file_list.csv") 
input_csv_path=os.path.join(output_dir,"total_eclipse_data_4readme.csv") 
directory_path=os.path.join(base_path, "Staging_Area")
print(input_csv_path)
error_log_path=os.path.join(output_dir,"csv_template_error.txt")
directory_path=zipPath
template_csv_path=os.path.join(resources_dir,"file_list_Template.csv")
result_out= create_file_list(
    directory_path,
    template_csv_path,
    output_csv_path,
    calculate_sha512=calculate_sha512,
)
print(result_out)