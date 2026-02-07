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

input_csv_path=os.path.join(output_dir,"total_eclipse_data_4readme.csv") 
print(input_csv_path)
error_log_path=os.path.join(output_dir,"csv_template_error.txt")

result_out = generate_readme_file(
    csv_file=input_csv_path,
    template_file=os.path.join(resources_dir,"README_template.html"),
    output_file=os.path.join(output_dir, "README.html"),
    error_log_file=error_log_path,
    save_markdown=True,
    )

print(result_out)