import os
import configparser

# 1. Load configuration from your .ini file
config_file = 'config1.ini'
config = configparser.ConfigParser()
config.read(config_file)

# 2. Extract folder names using the exact keys from your config 
STEP1_IN  = config.get('SnakemakeFolders', 'snakemake_step1_input')
STEP1_OUT = config.get('SnakemakeFolders', 'snakemake_step1_output')
FINAL_OUT = config.get('SnakemakeFolders', 'snakemake_final_output')

# 3. Identify all samples based on .svo2 files in the input folder 
SAMPLES, = glob_wildcards(os.path.join(STEP1_IN, "{sample}.svo2"))

rule all:
    input:
        expand(os.path.join(FINAL_OUT, "{sample}.mp4"), sample=SAMPLES),
        expand(os.path.join(FINAL_OUT, "{sample}.csv"), sample=SAMPLES)#,
        #expand(os.path.join(FINAL_OUT, "{sample}.json"), sample=SAMPLES)

# Step 1: Process SVO to MP4 and move the CSV to the output folder [cite: 1, 2]
rule process_svo:
    input:
        svo = os.path.join(STEP1_IN, "{sample}.svo2"),
        csv = os.path.join(STEP1_IN, "{sample}.csv")
    output:
        mp4 = os.path.join(STEP1_OUT, "{sample}.mp4"),
        csv_moved = os.path.join(STEP1_OUT, "{sample}.csv")
    threads: 2
    shell:
        """
        python run_pipeline1.py {input.svo} {STEP1_OUT} && \
        mv {input.csv} {output.csv_moved}
        """

# Step 2: Upload both files to AWS and move them to the final folder [cite: 3]
rule upload_to_aws:
    input:
        mp4 = os.path.join(STEP1_OUT, "{sample}.mp4"),
        csv = os.path.join(STEP1_OUT, "{sample}.csv")
    output:
        mp4_final = os.path.join(FINAL_OUT, "{sample}.mp4"),
        csv_final = os.path.join(FINAL_OUT, "{sample}.csv")
    threads: 1
    shell:
        """
        ./to_aws.sh {input.mp4} && \
        ./to_aws.sh {input.csv}
        """