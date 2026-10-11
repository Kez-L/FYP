#include <libxml/parser.h>
#include <libxml/tree.h>
#include <stdio.h>

int main(int argc, char **argv)
{
    if (argc < 2) {
        return 1;
    }

    /*
     * Parse the XML file supplied by AFL++.
     *
     * NONET prevents network access.
     * NOERROR/NOWARNING prevent malformed fuzz inputs
     * from flooding the terminal.
     */
    xmlDocPtr doc = xmlReadFile(
        argv[1],
        NULL,
        XML_PARSE_NONET |
        XML_PARSE_NOERROR |
        XML_PARSE_NOWARNING
    );

    if (doc != NULL) {

        /*
         * Access the document root so that successfully
         * parsed inputs exercise some additional libxml2 logic.
         */
        xmlNodePtr root = xmlDocGetRootElement(doc);

        if (root != NULL) {
            volatile int node_type = root->type;
            (void)node_type;
        }

        xmlFreeDoc(doc);
    }

    xmlCleanupParser();

    return 0;
}
