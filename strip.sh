for i in `find problem-set-full -name "answer.txt"`; do
  echo $i
  #sed -i '1{/```/d}' ${i}
  sed -i '${/```/d}' ${i}
done
