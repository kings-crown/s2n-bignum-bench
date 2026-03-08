import csv 
import os 
import sys

if len(sys.argv) != 3:
    print(f"Usage: python3 {sys.argv[0]} <input-csv> <output-dir>")
    sys.exit(1)

csv_path = sys.argv[1]
out_dir = sys.argv[2]

with open(csv_path) as f:
    for row in csv.DictReader(f):
        prob_dir = os.path.join(out_dir, row['problem_id'])
        os.makedirs(prob_dir, exist_ok=True)
        with open(os.path.join(prob_dir, 'answer.txt'), 'w') as af:
            af.write(row['answer'])
