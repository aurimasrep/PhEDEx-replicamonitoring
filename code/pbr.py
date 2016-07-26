#!/usr/bin/env python
#-*- coding: utf-8 -*-
#pylint: disable=
"""
File       		: pbr.py
Author     		: Aurimas Repecka <aurimas.repecka AT gmail dot com>
Based On Work By   	: Valentin Kuznetsov <vkuznet AT gmail dot com>
Description:
    http://stackoverflow.com/questions/29936156/get-csv-to-spark-dataframe
"""

# system modules
import os
import sys
import argparse

from pyspark import SparkContext
from pyspark.sql import SQLContext
from pyspark.sql.types import DoubleType, IntegerType

import re
from datetime import datetime as dt

# additional data needed for joins
GROUP_CSV_PATH = "additional_data/phedex_groups.csv"												# user group names
NODE_CSV_PATH = "additional_data/phedex_node_kinds.csv"												# node kinds

AGGREGATIONS = ["sum", "count", "min", "max", "first", "last", "mean"]  							# supported aggregation functions
GROUPKEYS = ["now", "dataset_name", "block_name", "node_name", "br_is_custiodial", "br_user_group",
			"data_tier", "acquisition_era", "node_kind", "now_sec"]									# supported group key values
GROUPRES = ["block_files", "block_bytes", "br_src_files", "br_src_bytes", "br_dest_files", 
			"br_dest_bytes", "br_node_files", "br_node_bytes", "br_xfer_files", "br_xfer_bytes"] 	# supported group result values

class OptionParser():
	def __init__(self):
		"User based option parser"
		self.parser = argparse.ArgumentParser(prog='PROG')
		msg = "Input data file on HDFS, e.g. hdfs:///path/data/file"
		self.parser.add_argument("--fname", action="store",
			dest="fname", default="", help=msg)
		msg = 'Output file on HDFS, e.g. hdfs:///path/data/output.file'
		self.parser.add_argument("--fout", action="store",
			dest="fout", default="", help=msg)
		self.parser.add_argument("--verbose", action="store_true",
			dest="verbose", default=False, help="Be verbose")
		self.parser.add_argument("--yarn", action="store_true",
			dest="yarn", default=False, help="Be yarn")
		self.parser.add_argument("--basedir", action="store",
			dest="basedir", default="/project/awg/cms/phedex/block-replicas-snapshots/csv/", help="Base directory of snapshots")
		self.parser.add_argument("--fromdate", action="store",
			dest="fromdate", default="", help="Filter by start date")
		self.parser.add_argument("--todate", action="store",
			dest="todate", default="", help="Filter by end date")
		self.parser.add_argument("--keys", action="store",
			dest="keys", default="dataset_name, node_name", help="Names (csv) of group keys to use, supported keys: %s" % GROUPKEYS)
		self.parser.add_argument("--results", action="store",
			dest="results", default="block_files, block_bytes", help="Names (csv) of group results to use, supported results: %s" % GROUPRES)
		self.parser.add_argument("--aggregations", action="store",
			dest="aggregations", default="sum", help="Names (csv) of aggregation functions to use, supported aggregations: %s" % AGGREGATIONS)
		self.parser.add_argument("--order", action="store",
			dest="order", default="", help="Column names (csv) for ordering data")
		self.parser.add_argument("--asc", action="store",
			dest="asc", default="", help="1 or 0 (csv) for ordering columns (0-desc, 1-asc)")

def headers():
	names = """now_sec, dataset_name, dataset_id, dataset_is_open, dataset_time_create, dataset_time_update,block_name, block_id, block_files, block_bytes, block_is_open, block_time_create, block_time_update,node_name, node_id, br_is_active, br_src_files, br_src_bytes, br_dest_files, br_dest_bytes,br_node_files, br_node_bytes, br_xfer_files, br_xfer_bytes, br_is_custodial, br_user_group_id, replica_time_create, replica_time_updater, br_user_group, node_kind, acquisition_era, data_tier, now"""
	return [n.strip() for n in names.split(',')]

# checks if value is empty
def isEmptyValue(value):
	return value == "" or value == "null" or not value

# checks if given data is empty
def isEmpty(data):
	print(data)
	if hasattr(data, '__iter__'):
		return any(isEmptyValue(element) for element in data)
	else:
		return isEmptyValue(data) 

# converts key value tuples to string representation
def toStringVal(item):
	return ','.join(str(i) for i in item) if hasattr(item, '__iter__') else str(item)

# prints aggregation results
def printVal(rdd, count, headers):   
	if headers:
		print headers

	iteration = 0
	for item in rdd.collect():
		print toStringVal(item)
		iteration += 1
		if iteration > count:
			break    

# splits string into given groups by compiled pattern
def splitToGroups(src, pattern, pgroups):
	matching = pattern.search(src)	

	output = []
	if matching:
		for pgroup in pgroups:
			output.append(matching.group(pgroup))
	else:
		output = ["null"] * len(pgroups)

	return output

# get dictionaries needed for joins
def getJoinDic():   
	groupdic = {"null" : "null"}
	with open(GROUP_CSV_PATH) as fg:
		for line in fg.read().splitlines():
			(gid, gname) = line.split(',')
			groupdic[gid] = gname

	nodedic = {"null" : "null"}
	with open(NODE_CSV_PATH) as fn:
		for line in fn.read().splitlines():
			data = line.split(',')
			nodedic[data[0]] = data[2] 

	return groupdic, nodedic  

# get file list by dates
def getFileList(basedir, fromdate, todate):
	dirs = os.popen("hadoop fs -ls %s | sed '1d;s/  */ /g' | cut -d\  -f8" % basedir).read().splitlines()
	# if files are not in hdfs --> dirs = os.listdir(basedir)
	
	try:
		fromdate = dt.strptime(fromdate, "%Y-%m-%d")
		todate = dt.strptime(todate, "%Y-%m-%d")
	except ValueError as err:
		raise ValueError("Unparsable date parameters. Date should be specified in form: YYYY-mm-dd")		
 		
	pattern = re.compile(r"(\d{4}-\d{2}-\d{2})")
   
	dirdate_dic = {}
	for di in dirs:
		matching = pattern.search(di)
		if matching:
			dirdate_dic[di] = dt.strptime(matching.group(1), "%Y-%m-%d")

	# if files are not in hdfs --> return [ basedir + k for k, v in dirdate_dic.items() if v >= fromdate and v <= todate]
	return [k for k, v in dirdate_dic.items() if v >= fromdate and v <= todate]		

# validate aggregation parameters
def validateAggregationParams(keys, res, agg, order):
	unsup_keys = set(keys).difference(set(GROUPKEYS)) 
	unsup_res = set(res).difference(set(GROUPRES))
	unsup_agg = set(agg).difference(set(AGGREGATIONS))
	unsup_ord = set(order).difference(set(keys + res)) if order != [''] else None
	
	msg = ""
	if unsup_keys:
		msg += 'Group key(s) = "%s" are not supported. ' % toStringVal(unsup_keys)
	if unsup_res:
		msg += 'Group result(s) = "%s" are not supported. ' % toStringVal(unsup_res)
	if unsup_agg:
		msg += 'Aggregation function(s) = "%s" are not supported. ' % toStringVal(unsup_agg)
	if unsup_ord:
		msg += 'Order key(s) = "%s" are not available. ' % toStringVal(unsup_ord)
	if msg:
		raise NotImplementedError(msg)

# validate dates and fill default values		
def validateDates(fromdate, todate):
	if not fromdate or not todate:
		fromdate = dt.strftime(dt.now(), "%Y-%m-%d")
		todate = dt.strftime(dt.now(), "%Y-%m-%d")
	return fromdate, todate

# creating results and aggregation dictionary
def zipResultAgg(res, agg):
	if len(res) == len(agg):
		return dict(zip(res, agg))
	else:
		return dict(zip(res, agg * len(res)))

# form ascennding and order arrays according aggregation functions
def formOrdAsc(order, asc, resAgg_dic):
	asc = map(int, asc) if len(order) == len(asc) else [1] * len(order)
	orderN = [resAgg_dic[orde] + "(" + orde + ")" if orde in resAgg_dic.keys() else orde for orde in order] 
	return orderN, asc

#########################################################################################################################################

def main():
	"Main function"
	optmgr  = OptionParser()
	opts = optmgr.parser.parse_args()

    # setup spark/sql context to be used for communication with HDFS
	sc = SparkContext(appName="phedex_br")
	if not opts.yarn:
		sc.setLogLevel("ERROR")
	sqlContext = SQLContext(sc)

    # read given file(s) into RDD
	if opts.fname:
		rdd = sc.textFile(opts.fname).map(lambda line: line.split(","))
	elif opts.basedir:
		fromdate, todate = validateDates(opts.fromdate, opts.todate)
		files = getFileList(opts.basedir, fromdate, todate)
		msg = "Between dates %s and %s found %d directories" % (fromdate, todate, len(files))
		print msg

		if not files:
			return
		rdd = sc.union([sc.textFile(file_path).map(lambda line: line.split(",")) for file_path in files])
	else:
		raise ValueError("File or directory not specified. Specify fname or basedir parameters.")

	# parsing additional data (to given data adding: group name, node kind, acquisition era, data tier, now days)
	groupdic, nodedic = getJoinDic()

	pattern = re.compile(r""" ^/[^/]*                         # PrimaryDataset
		     			 /(?P<AcquisitionEra>[^/^-]*)-[^/]*   # AcquisitionEra-ProcessingEra
                   	     /(?P<DataTier>[^/]*)$                # DataTier """, re.X)			# compile is used for efficiency as regex will be used many times   
	groups = ["AcquisitionEra", "DataTier"]

	head_arr = headers()
	dname_index = head_arr.index("dataset_name")
	gid_index = head_arr.index("br_user_group_id")
	nid_index = head_arr.index("node_id")
	now_index = head_arr.index("now_sec")
	nrd = rdd.map(lambda r: (r + [groupdic[r[gid_index]]] + \
								 [nodedic[r[nid_index]]] + \
								 splitToGroups(r[dname_index], pattern, groups) + \
							 	 [float(r[now_index]) / 86400] ))

    # create a dataframe out of RDD
	pdf = nrd.toDF(head_arr)
	if opts.verbose:
		pdf.show()
		print("pdf data type", type(pdf))
		pdf.printSchema()

    # cast columns to correct data types
	ndf = pdf.withColumn("block_bytes_tmp", pdf.block_bytes.cast(DoubleType()))\
			.drop("block_bytes").withColumnRenamed("block_bytes_tmp", "block_bytes")\
			.withColumn("block_files_tmp", pdf.block_files.cast(IntegerType()))\
			.drop("block_files").withColumnRenamed("block_files_tmp", "block_files")\
			.withColumn("br_src_bytes_tmp", pdf.br_src_bytes.cast(DoubleType()))\
			.drop("br_src_bytes").withColumnRenamed("br_src_bytes_tmp", "br_src_bytes")\
			.withColumn("br_src_files_tmp", pdf.br_src_files.cast(IntegerType()))\
			.drop("br_src_files").withColumnRenamed("br_src_files_tmp", "br_src_files")\
			.withColumn("br_dest_bytes_tmp", pdf.br_dest_bytes.cast(DoubleType()))\
			.drop("br_dest_bytes").withColumnRenamed("br_dest_bytes_tmp", "br_dest_bytes")\
			.withColumn("br_dest_files_tmp", pdf.br_dest_files.cast(IntegerType()))\
			.drop("br_dest_files").withColumnRenamed("br_dest_files_tmp", "br_dest_files")\
			.withColumn("br_node_bytes_tmp", pdf.br_node_bytes.cast(DoubleType()))\
			.drop("br_node_bytes").withColumnRenamed("br_node_bytes_tmp", "br_node_bytes")\
			.withColumn("br_node_files_tmp", pdf.br_node_files.cast(IntegerType()))\
			.drop("br_node_files").withColumnRenamed("br_node_files_tmp", "br_node_files")\
			.withColumn("br_xfer_bytes_tmp", pdf.br_xfer_bytes.cast(DoubleType()))\
			.drop("br_xfer_bytes").withColumnRenamed("br_xfer_bytes_tmp", "br_xfer_bytes")\
			.withColumn("br_xfer_files_tmp", pdf.br_xfer_files.cast(IntegerType()))\
			.drop("br_xfer_files").withColumnRenamed("br_xfer_files_tmp", "br_xfer_files")\
			.withColumn("now_tmp", pdf.now.cast(IntegerType()))\
			.drop("now").withColumnRenamed("now_tmp", "now")

    # process aggregation parameters
	keys = [key.lower().strip() for key in opts.keys.split(',')]
	results = [result.lower().strip() for result in opts.results.split(',')]
	aggregations = [agg.strip() for agg in opts.aggregations.split(',')]
	order = [orde.strip() for orde in opts.order.split(',')] if opts.order else []
	asc = [asce.strip() for asce in opts.asc.split(',')] if opts.order else []

	validateAggregationParams(keys, results, aggregations, order)
	
	resAgg_dic = zipResultAgg(results, aggregations)
	order, asc = formOrdAsc(order, asc, resAgg_dic)

	# perform aggregation
	if order:
		aggres = ndf.groupBy(keys).agg(resAgg_dic).orderBy(order, ascending=asc)
	else:
		aggres = ndf.groupBy(keys).agg(resAgg_dic)

	# output results
	if opts.fout:
		print(toStringVal(aggres.schema.names))	# print schema that was created dynamically
		lines = aggres.map(toStringVal)
		lines.saveAsTextFile(opts.fout)
	else:
		printVal(aggres, 15, toStringVal(aggres.schema.names))

if __name__ == '__main__':
	main()

